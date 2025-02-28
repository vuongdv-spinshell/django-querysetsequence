import functools
from collections import defaultdict
from itertools import dropwhile
from operator import __not__, attrgetter, eq, ge, gt, itemgetter, le, lt, mul

import django
from django.core.exceptions import (
    FieldError,
    MultipleObjectsReturned,
    ObjectDoesNotExist,
)
from django.db import connection, transaction
from django.db.models.base import Model
from django.db.models.constants import LOOKUP_SEP
from django.db.models.query import EmptyQuerySet, QuerySet

# Only export the public API for QuerySetSequence. (Note that QuerySequence and
# QuerySetSequenceModel are considered semi-public: the APIs probably won't
# change, but implementation is not guaranteed. Other functions/classes are
# considered implementation details.)
__all__ = ["QuerySetSequence"]


def cmp(a, b):
    """Compare two arbitrary values of the same type."""
    if a is None and b is None:
        return 0
    elif a is None:
        # TODO If different QuerySets pull from different databases then connection
        # might be wrong and this might do weird things.
        if connection.features.nulls_order_largest:
            return 1
        return -1
    elif b is None:
        if connection.features.nulls_order_largest:
            return -1
        return 1
    return (a > b) - (a < b)


def multiply_iterables(it1, it2):
    """
    Element-wise iterables multiplications.
    """
    assert len(it1) == len(
        it2
    ), "Can not element-wise multiply iterables of different length."
    return list(map(mul, it1, it2))


def cumsum(seq):
    s = 0
    for c in seq:
        s += c
        yield s


class BaseIterable:
    def __init__(self, querysetsequence):
        # Create a clone so that subsequent calls to iterate are kept separate.
        self._querysets = querysetsequence._querysets
        self._queryset_idxs = querysetsequence._queryset_idxs
        self._order_by = querysetsequence._order_by
        self._standard_ordering = querysetsequence._standard_ordering
        self._low_mark = querysetsequence._low_mark
        self._high_mark = querysetsequence._high_mark

    @staticmethod
    def _get_fields(obj, *field_names):
        """Retrieve the values of fields from the object."""
        raise NotImplementedError()

    def _add_queryset_index(self, obj, value):
        """Add the QuerySet index to the object and return the object."""
        raise NotImplementedError()

    @classmethod
    def _get_field_names(cls, model):
        """Return a list of field names that are part of a model."""
        return [f.name for f in model._meta.get_fields()]

    @classmethod
    def _cmp(cls, value1, value2):
        """
        Comparison method that takes into account Django's special rules when
        ordering by a field that is a model:

            1. Try following the default ordering on the related model.
            2. Order by the model's primary key, if there is no Meta.ordering.

        """
        if isinstance(value1, Model) and isinstance(value2, Model):
            field_names = value1._meta.ordering

            # Assert that the ordering is the same between different models.
            if field_names != value2._meta.ordering:
                valid_field_names = set(cls._get_field_names(value1)) & set(
                    cls._get_field_names(value2)
                )
                raise FieldError(
                    "Ordering differs between models. Choices are: %s"
                    % ", ".join(valid_field_names)
                )

            # By default, order by the pk.
            if not field_names:
                field_names = ["pk"]

            # TODO Figure out if we don't need to generate this comparator every
            # time.
            return cls._generate_comparator(field_names)(value1, value2)

        return cmp(value1, value2)

    @classmethod
    def _generate_comparator(cls, field_names):
        """
        Construct a comparator function based on the field names. The comparator
        returns the first non-zero comparison value.

        Inputs:
            field_names (iterable of strings): The field names to sort on.

        Returns:
            A comparator function.
        """
        # Ensure that field names is a list and not a tuple.
        field_names = list(field_names)

        # For fields that start with a '-', reverse the ordering of the
        # comparison.
        reverses = [1] * len(field_names)
        for i, field_name in enumerate(field_names):
            if field_name[0] == "-":
                reverses[i] = -1
                field_names[i] = field_name[1:]

        field_names = [f.replace(LOOKUP_SEP, ".") for f in field_names]

        def comparator(i1, i2):
            # Get a tuple of values for comparison.
            v1 = cls._get_fields(i1, *field_names)
            v2 = cls._get_fields(i2, *field_names)

            # If there's only one arg supplied, a single item is returned,
            # directly return the result in this case.
            if len(field_names) == 1:
                return cls._cmp(v1, v2) * reverses[0]

            # Compare each field for the two items, reversing if necessary.
            order = multiply_iterables(list(map(cls._cmp, v1, v2)), reverses)

            try:
                # The first non-zero element.
                return next(dropwhile(__not__, order))
            except StopIteration:
                # Everything was equivalent.
                return 0

        return comparator

    def _ordered_iterator(self):
        """
        Interleave the values of each QuerySet in order to handle the requested
        ordering. Also adds the '#' property to each returned item.
        """
        # A list of tuples, each with:
        #   * The iterable
        #   * The QuerySet number
        #   * The next value
        #
        # (Remember that each QuerySet is already sorted.)
        iterables = []
        for i, qs in zip(self._queryset_idxs, self._querysets):
            it = iter(qs)
            try:
                value = next(it)
            except StopIteration:
                # If this is already empty, just skip it.
                continue
            # Set the QuerySet number so that the comparison works properly.
            value = self._add_queryset_index(value, i)
            iterables.append((it, i, value))

        # The offset of items returned.
        index = 0

        # Create a comparison function based on the requested ordering.
        _comparator = self._generate_comparator(self._order_by)

        def comparator(tuple_1, tuple_2):
            # The last element in each tuple is the actual item to compare.
            return _comparator(tuple_1[2], tuple_2[2])

        comparator = functools.cmp_to_key(comparator)

        # If in reverse mode, get the last value instead of the first value from
        # ordered_values below.
        if self._standard_ordering:
            next_value_ind = 0
        else:
            next_value_ind = -1

        # Continue until all iterables are empty.
        while iterables:
            # If there's only one iterator left, don't bother sorting.
            if len(iterables) > 1:
                # Sort the current values for each iterable.
                iterables = sorted(iterables, key=comparator)

                # The next ordering item is in the first position, unless we're
                # in reverse mode.
                it, i, value = iterables[next_value_ind]
            else:
                it, i, value = iterables[0]

            # Return the next value if we're within the slice of interest.
            if self._low_mark <= index:
                yield value
            index += 1
            # We've left the slice of interest, we're done.
            if index == self._high_mark:
                return

            # Iterate the iterable that just lost a value.
            try:
                value = next(it)
                # Set the QuerySet number so that the comparison works properly.
                value = self._add_queryset_index(value, i)
                iterables[next_value_ind] = it, i, value
            except StopIteration:
                # This iterator is done, remove it.
                del iterables[next_value_ind]

    def _unordered_iterator(self):
        """
        Return the value of each QuerySet, but also add the '#' property to each
        return item.
        """
        for i, qs in zip(self._queryset_idxs, self._querysets):
            for item in qs:
                yield self._add_queryset_index(item, i)

    def __iter__(self):
        # If there's no QuerySets, just return an empty iterator.
        if not len(self._querysets):
            return iter([])

        # If order is necessary, evaluate and start feeding data back.
        if self._order_by:
            # If the first element of order_by is '#', this means first order by
            # QuerySet. If it isn't this, then returned the interleaved
            # iterator.
            if self._order_by[0].lstrip("-") != "#":
                return self._ordered_iterator()

            # Otherwise, order by QuerySet first. Handle reversing the
            # QuerySets, if necessary.
            elif self._order_by[0].startswith("-"):
                self._querysets = self._querysets[::-1]

        # If there is no ordering, or the ordering is specific to each QuerySet,
        # evaluation can be pushed off further.

        # If there is no slicing, iterate through each QuerySet. This avoids
        # calling count() on each QuerySet.
        if self._low_mark == 0 and self._high_mark is None:
            return self._unordered_iterator()

        # First trim any QuerySets based on the currently set limits!
        counts = [0]
        counts.extend(cumsum([it.count() for it in self._querysets]))

        # Trim the beginning of the QuerySets, if necessary.
        start_index = 0
        low_mark, high_mark = self._low_mark, self._high_mark
        if low_mark != 0:
            # Convert a negative index into a positive.
            if low_mark < 0:
                low_mark += counts[-1]

            # Find the point when low_mark crosses a threshold.
            for i, offset in enumerate(counts):
                if offset <= low_mark:
                    start_index = i
                if low_mark < offset:
                    break

        # Trim the end of the QuerySets, if necessary.
        end_index = len(self._querysets)
        if high_mark is None:
            # If it was unset (meaning all), set it to the maximum.
            high_mark = counts[-1]
        elif high_mark:
            # Convert a negative index into a positive.
            if high_mark < 0:
                high_mark += counts[-1]

            # Find the point when high_mark crosses a threshold.
            for i, offset in enumerate(counts):
                if high_mark <= offset:
                    end_index = i
                    break

        # Remove QuerySets we don't care about.
        self._querysets = self._querysets[start_index:end_index]
        self._queryset_idxs = self._queryset_idxs[start_index:end_index]

        # The low_mark needs the removed QuerySets subtracted from it.
        low_mark -= counts[start_index]
        # The high_mark needs the count of all QuerySets before it subtracted
        # from it.
        high_mark -= counts[end_index - 1]

        # Apply the offsets to the edge QuerySets (apply the high mark first
        # in-case there's only a single QuerySet left).
        self._querysets[-1] = self._querysets[-1][:high_mark]
        self._querysets[0] = self._querysets[0][low_mark:]

        # For anything left, just iterate through each QuerySet.
        return self._unordered_iterator()


class ModelIterable(BaseIterable):
    @staticmethod
    def _get_fields(obj, *field_names):
        return attrgetter(*field_names)(obj)

    def _add_queryset_index(self, obj, value):
        # For models, always add the QuerySet index.
        setattr(obj, "#", value)
        return obj


class ValuesIterable(BaseIterable):
    def __init__(self, querysetsequence):
        super().__init__(querysetsequence)

        self._fields = querysetsequence._fields
        # If no fields are specified (or if '#' is explicitly specified) include
        # the QuerySet index.
        self._include_qs_index = not self._fields or "#" in self._fields
        self._remove_fields = False

        # If there are any "order_by" fields which are *not* the fields to be
        # returned, they also need to be captured.
        if self._order_by:
            qss_fields, std_fields = querysetsequence._separate_fields(*self._fields)
            qss_order_fields, std_order_fields = querysetsequence._separate_fields(
                *self._order_by
            )
            extra_fields = [
                f for f in std_order_fields if f.lstrip("-") not in std_fields
            ]

            self._querysets = [
                qs.values(*std_fields, *extra_fields) for qs in self._querysets
            ]

            # If any additional fields are pulled, they'll need to be removed.
            self._include_qs_index |= bool(qss_order_fields)
            self._remove_fields = bool(extra_fields) or bool(qss_fields)

    def __iter__(self):
        if not self._remove_fields:
            yield from super().__iter__()
            return

        # The extra fields added for ordering need to be removed.
        for it in super().__iter__():
            yield {k: it[k] for k in self._fields}

    @staticmethod
    def _get_fields(obj, *field_names):
        return itemgetter(*field_names)(obj)

    def _add_queryset_index(self, obj, value):
        if self._include_qs_index:
            obj["#"] = value
        return obj


class ValuesListIterable(BaseIterable):
    def __init__(self, querysetsequence):
        super().__init__(querysetsequence)

        fields = querysetsequence._fields
        # Only keep the values from fields.
        self._last_field = len(fields)
        # The location of the QuerySet index.
        try:
            self._qs_index = fields.index("#")
        except ValueError:
            self._qs_index = None

        # If there are any "order_by" fields which are *not* the fields to be
        # returned, they also need to be captured.
        if self._order_by:
            # Find any fields which are only used for ordering.
            _, std_fields = querysetsequence._separate_fields(*fields)
            _, std_order_fields = querysetsequence._separate_fields(*self._order_by)
            order_only_fields = [
                f for f in std_order_fields if f.lstrip("-") not in std_fields
            ]

            # Capture both the fields to return as well as the fields used only
            # for ordering.
            all_fields = std_fields + order_only_fields
            self._querysets = [
                qs.values_list(*std_fields, *order_only_fields)
                for qs in self._querysets
            ]

            # If one of the returned fields is the QuerySet index, insert it so
            # that the indexes of the fields after it are correct.
            if self._qs_index:
                all_fields = (
                    all_fields[: self._qs_index] + ("#",) + all_fields[self._qs_index :]
                )

            # Convert the order_by field names into indexes, but encoded as strings.
            #
            # Note that this assumes that the ordering of the values is shallow
            # (i.e. nothing returns a Model instance).
            order_by_indexes = []
            for field in self._order_by:
                field_name = field.lstrip("-")

                if field_name == "#":
                    # If the index is not one of the returned fields, add it as
                    # the last field.
                    if not self._qs_index:
                        self._qs_index = len(all_fields)
                    field_index = self._qs_index
                else:
                    field_index = all_fields.index(field_name)

                order_by_indexes.append(
                    ("-" if field[0] == "-" else "") + str(field_index)
                )
            self._order_by = order_by_indexes

    def __iter__(self):
        # If there's no particular ordering, do not rebuild the tuples.
        if not self._order_by:
            yield from super().__iter__()
            return

        # Remove the fields only used for ordering from the result.
        for row in super().__iter__():
            yield row[: self._last_field]

    @staticmethod
    def _get_fields(obj, *field_names):
        # Note that BaseIterable.__iter__ strips '-' before getting here, so all
        # indexes are positive.
        field_indexes = [int(f) for f in field_names]
        return itemgetter(*field_indexes)(obj)

    def _add_queryset_index(self, obj, value):
        # If the QuerySet index needs to be inserted, build a new tuple with it.
        if self._qs_index is None:
            return obj
        return obj[: self._qs_index] + (value,) + obj[self._qs_index :]


class FlatValuesListIterable(ValuesListIterable):
    def __iter__(self):
        # Flat values lists can only have a single value in them, yield it.
        for row in super().__iter__():
            yield row[0]


class NamedValuesListIterable(ValuesListIterable):
    pass


class ProxyModel:
    """
    Wrapper for generating DoesNotExist exceptions without modifying
    the provided model. This is needed by DRF as the views only handle
    the specific exception by the model.
    """

    def __init__(self, model=None):
        self._model = model

        # Define DoesNotExist for abstract models.
        if self._model and not self._model._meta.abstract:
            self.DoesNotExist = model.DoesNotExist
        else:
            self.DoesNotExist = ObjectDoesNotExist

    def __getattr__(self, name):
        return getattr(self._model, name)


class QuerySetSequence:
    """
    Wrapper for multiple QuerySets without the restriction on the identity of
    the base models.

    """

    def __init__(self, *args, model=None):
        self._set_querysets(args)
        # Some information necessary for properly iterating through a QuerySet.
        self._order_by = []
        self._fields = None
        self._standard_ordering = True
        self._low_mark, self._high_mark = 0, None

        self._iterable_class = ModelIterable
        self._result_cache = None

        self.model = ProxyModel(model)

    def _set_querysets(self, querysets):
        self._querysets = list(querysets)
        # The original ordering of the QuerySets.
        self._queryset_idxs = list(range(len(self._querysets)))

    def _clone(self):
        clone = QuerySetSequence(*[qs._clone() for qs in self._querysets])
        clone._queryset_idxs = self._queryset_idxs
        clone._order_by = self._order_by
        clone._fields = self._fields
        clone._standard_ordering = self._standard_ordering
        clone._low_mark = self._low_mark
        clone._high_mark = self._high_mark
        clone._iterable_class = self._iterable_class
        clone.model = self.model

        return clone

    def _fetch_all(self):
        if self._result_cache is None:
            self._result_cache = list(self._iterable_class(self))

    @property
    def _prefetch_related_lookups(self):
        # A hack for ModelChoiceField, which uses internal APIs from QuerySet.
        # This should be fixed when https://code.djangoproject.com/ticket/29984
        # is fixed.
        #
        # Note that this really just needs to return a truth-y value if any of
        # the QuerySets are using prefetch_related, but this tries to keep the
        # type sane at least.
        result = ()
        for qs in self._querysets:
            result += qs._prefetch_related_lookups
        return result

    # Python magic methods.

    def __len__(self):
        self._fetch_all()
        return len(self._result_cache)

    def __iter__(self):
        self._fetch_all()
        return iter(self._result_cache)

    def __bool__(self):
        self._fetch_all()
        return bool(self._result_cache)

    def __getitem__(self, k):
        """
        Retrieves an item or slice from the set of results.
        """
        if not isinstance(k, (int, slice)):
            raise TypeError
        assert (not isinstance(k, slice) and (k >= 0)) or (
            isinstance(k, slice)
            and (k.start is None or k.start >= 0)
            and (k.stop is None or k.stop >= 0)
        ), "Negative indexing is not supported."

        if isinstance(k, slice):
            qs = self._clone()
            # If start is not given, it is 0.
            if k.start is not None:
                start = int(k.start)
            else:
                start = 0
            # Apply the new start to any previous slices.
            qs._low_mark += start

            # If stop is not given, don't modify the stop.
            if k.stop is not None:
                stop = int(k.stop)

                # The high mark needs to take into an account any previous
                # offsets of the low mark.
                offset = stop - start
                qs._high_mark = qs._low_mark + offset
            return list(qs)[:: k.step] if k.step else qs

        qs = self._clone()
        qs._low_mark += k
        qs._high_mark = qs._low_mark + 1
        return list(qs)[0]

    def __and__(self, other):
        # If the other QuerySet is an EmptyQuerySet, this is a no-op.
        if isinstance(other, EmptyQuerySet):
            return other
        combined = self._clone()

        querysets = []
        for qs in combined._querysets:
            # Only QuerySets of the same type can have any overlap.
            if issubclass(qs.model, other.model):
                querysets.append(qs & other)

        # If none are left, we're left with an EmptyQuerySet.
        if not querysets:
            return other.none()

        combined._set_querysets(querysets)
        return combined

    def __or__(self, other):
        # If the other QuerySet is an EmptyQuerySet, this is a no-op.
        if isinstance(other, EmptyQuerySet):
            return self
        combined = self._clone()

        # If the other instance is a QuerySetSequence, combine the QuerySets.
        if isinstance(other, QuerySetSequence):
            combined._set_querysets(self._querysets + other._querysets)

        elif isinstance(other, QuerySet):
            combined._set_querysets(self._querysets + [other])

        return combined

    def _separate_filter_fields(self, **kwargs):
        qss_fields, std_fields = self._separate_fields(*kwargs.keys())

        # Remove any fields that start with '#' from kwargs.
        qss_kwargs = {
            field: value for field, value in kwargs.items() if field in qss_fields
        }
        std_kwargs = {
            field: value for field, value in kwargs.items() if field in std_fields
        }

        return qss_kwargs, std_kwargs

    def _separate_fields(self, *fields):
        # Remove any fields that start with '#' from kwargs.
        qss_fields = []
        std_fields = []
        for field in fields:
            if field.startswith("#") or field.startswith("-#"):
                qss_fields.append(field)
            else:
                std_fields.append(field)

        return qss_fields, std_fields

    def _filter_or_exclude_querysets(self, negate, **kwargs):
        """
        Similar to QuerySet._filter_or_exclude, but run over the QuerySets in
        the QuerySetSequence instead of over each QuerySet's fields.
        """
        # Ensure negate is a boolean.
        negate = bool(negate)

        for kwarg, value in kwargs.items():
            parts = kwarg.split(LOOKUP_SEP)

            # Ensure this is being used to filter QuerySets.
            if parts[0] != "#":
                raise ValueError(
                    "Keyword '%s' is not a valid keyword to filter over, "
                    "it must begin with '#'." % kwarg
                )

            # Don't allow __ multiple times.
            if len(parts) > 2:
                raise ValueError(
                    "Keyword '%s' must not contain multiple "
                    "lookup separators." % kwarg
                )

            # The actual lookup is the second part.
            try:
                lookup = parts[1]
            except IndexError:
                lookup = "exact"

            # Math operators that all have the same logic.
            LOOKUP_TO_OPERATOR = {
                "exact": eq,
                "iexact": eq,
                "gt": gt,
                "gte": ge,
                "lt": lt,
                "lte": le,
            }
            try:
                operator = LOOKUP_TO_OPERATOR[lookup]

                # These expect integers, this matches the logic in
                # IntegerField.get_prep_value(). (Essentially treat the '#'
                # field as an IntegerField.)
                if value is not None:
                    value = int(value)

                self._queryset_idxs = filter(
                    lambda i: operator(i, value) != negate, self._queryset_idxs
                )
                continue
            except KeyError:
                # It wasn't one of the above operators, keep trying.
                pass

            # Some of these seem to get handled as bytes.
            if lookup in ("contains", "icontains"):
                value = str(value)
                self._queryset_idxs = filter(
                    lambda i: (value in str(i)) != negate, self._queryset_idxs
                )

            elif lookup == "in":
                self._queryset_idxs = filter(
                    lambda i: (i in value) != negate, self._queryset_idxs
                )

            elif lookup in ("startswith", "istartswith"):
                value = str(value)
                self._queryset_idxs = filter(
                    lambda i: str(i).startswith(value) != negate, self._queryset_idxs
                )

            elif lookup in ("endswith", "iendswith"):
                value = str(value)
                self._queryset_idxs = filter(
                    lambda i: str(i).endswith(value) != negate, self._queryset_idxs
                )

            elif lookup == "range":
                # Inclusive include.
                start, end = value
                self._queryset_idxs = filter(
                    lambda i: (start <= i <= end) != negate, self._queryset_idxs
                )

            else:
                # Any other field lookup is not supported, e.g. date, year, month,
                # day, week_day, hour, minute, second, isnull, search, regex, and
                # iregex.
                raise ValueError("Unsupported lookup '%s'" % lookup)

        # Convert back to a list on Python 3.
        self._queryset_idxs = list(self._queryset_idxs)

        # Finally, keep only the QuerySets we care about!
        self._querysets = [self._querysets[i] for i in self._queryset_idxs]

    # Methods that return new QuerySets
    def filter(self, *args, **kwargs):
        qss_fields, fields = self._separate_filter_fields(**kwargs)

        clone = self._clone()
        clone._filter_or_exclude_querysets(False, **qss_fields)
        clone._querysets = [qs.filter(*args, **fields) for qs in clone._querysets]
        return clone

    def exclude(self, **kwargs):
        qss_fields, fields = self._separate_filter_fields(**kwargs)

        clone = self._clone()
        clone._filter_or_exclude_querysets(True, **qss_fields)
        clone._querysets = [qs.exclude(**fields) for qs in clone._querysets]
        return clone

    def annotate(self, *args, **kwargs):
        clone = self._clone()
        clone._querysets = [qs.annotate(*args, **kwargs) for qs in clone._querysets]
        return clone

    if django.VERSION > (3, 2):

        def alias(self, *args, **kwargs):
            raise NotImplementedError()

    def order_by(self, *fields):
        _, filtered_fields = self._separate_fields(*fields)

        # Apply the filtered fields to each underlying QuerySet.
        clone = self._clone()
        clone._querysets = [qs.order_by(*filtered_fields) for qs in self._querysets]

        # But keep the original fields for the clone.
        clone._order_by = list(fields)
        return clone

    def reverse(self):
        clone = self._clone()
        clone._querysets = [qs.reverse() for qs in reversed(self._querysets)]
        clone._standard_ordering = not self._standard_ordering
        return clone

    def distinct(self, *fields):
        if len({qs.model for qs in self._querysets}) != len(self._querysets):
            raise NotImplementedError("Multiple QS of same model unsupported")
        clone = self._clone()
        clone._querysets = [qs.distinct() for qs in clone._querysets]
        return clone

    def values(self, *fields, **expressions):
        _, std_fields = self._separate_fields(*fields)

        clone = self._clone()
        clone._querysets = [
            qs.values(*std_fields, **expressions) for qs in self._querysets
        ]
        clone._fields = list(fields) + list(expressions.keys())
        clone._iterable_class = ValuesIterable

        return clone

    def values_list(self, *fields, flat=False, named=False):
        if flat and named:
            raise TypeError("'flat' and 'named' can't be used together.")
        if flat and len(fields) > 1:
            raise TypeError(
                "'flat' is not valid when values_list is called with more than "
                "one field."
            )

        _, std_fields = self._separate_fields(*fields)

        clone = self._clone()
        # Note that we always process the flat-ness ourself.
        clone._querysets = [
            qs.values_list(*std_fields, flat=False, named=named)
            for qs in self._querysets
        ]
        clone._fields = list(fields)
        clone._iterable_class = (
            NamedValuesListIterable
            if named
            else FlatValuesListIterable
            if flat
            else ValuesListIterable
        )

        return clone

    def dates(self, field, kind, order="ASC"):
        raise NotImplementedError()

    def datetimes(self, field_name, kind, order="ASC", tzinfo=None, is_dst=None):
        raise NotImplementedError()

    def none(self):
        # This is a bit odd, but use the first QuerySet to properly return an
        # that is an instance of EmptyQuerySet.
        return self._querysets[0].none()

    def all(self):
        clone = self._clone()
        clone._querysets = [qs.all() for qs in self._querysets]
        return clone

    def union(self, *other_qs, **kwargs):
        raise NotImplementedError()

    def intersection(self, *other_qs, **kwargs):
        raise NotImplementedError()

    def difference(self, *other_qs, **kwargs):
        raise NotImplementedError()

    def select_related(self, *fields):
        clone = self._clone()
        clone._querysets = [qs.select_related(*fields) for qs in self._querysets]
        return clone

    def prefetch_related(self, *lookups):
        clone = self._clone()
        clone._querysets = [qs.prefetch_related(*lookups) for qs in self._querysets]
        return clone

    def extra(
        self,
        select=None,
        where=None,
        params=None,
        tables=None,
        order_by=None,
        select_params=None,
    ):
        clone = self._clone()
        clone._querysets = [
            qs.extra(
                select=select,
                where=where,
                params=params,
                tables=tables,
                order_by=order_by,
                select_params=select_params,
            )
            for qs in self._querysets
        ]
        return clone

    def defer(self, *fields):
        clone = self._clone()
        clone._querysets = [qs.defer(*fields) for qs in self._querysets]
        return clone

    def only(self, *fields):
        clone = self._clone()
        clone._querysets = [qs.only(*fields) for qs in self._querysets]
        return clone

    def using(self, alias):
        clone = self._clone()
        clone._querysets = [qs.using(alias) for qs in self._querysets]
        return clone

    def select_for_update(self, nowait=False, skip_locked=False, of=(), no_key=False):
        raise NotImplementedError()

    def raw(self, raw_query, params=(), translations=None):
        raise NotImplementedError()

    # Methods that do not return QuerySets
    def get(self, **kwargs):
        clone = self.filter(**kwargs)

        result = None
        for qs in clone._querysets:
            try:
                obj = qs.get()
            except ObjectDoesNotExist:
                pass
            # Don't catch the MultipleObjectsReturned(), allow it to raise.
            else:
                # If a second object is found, raise an exception.
                if result:
                    raise MultipleObjectsReturned()
                result = obj

        # Checked all QuerySets and no object was found.
        if result is None:
            raise self.model.DoesNotExist()

        # Return the only result found.
        return result

    if django.VERSION >= (4, 1):

        async def aget(self, **kwargs):
            raise NotImplementedError()

    def create(self, **kwargs):
        raise NotImplementedError()

    if django.VERSION >= (4, 1):

        async def acreate(self, **kwargs):
            raise NotImplementedError()

    def get_or_create(self, defaults=None, **kwargs):
        raise NotImplementedError()

    if django.VERSION >= (4, 1):

        async def aget_or_create(self, defaults=None, **kwargs):
            raise NotImplementedError()

    def update_or_create(self, defaults=None, **kwargs):
        raise NotImplementedError()

    if django.VERSION >= (4, 1):

        async def aupdate_or_create(self, defaults=None, **kwargs):
            raise NotImplementedError()

    # Django 4.1 added additional parameters.
    if django.VERSION >= (4, 1):

        def bulk_create(
            self,
            objs,
            batch_size=None,
            ignore_conflicts=False,
            update_conflicts=False,
            update_fields=None,
            unique_fields=None,
        ):
            raise NotImplementedError()

        async def abulk_create(self, objs, batch_size=None, ignore_conflicts=False):
            raise NotImplementedError()

    else:

        def bulk_create(self, objs, batch_size=None, ignore_conflicts=False):
            raise NotImplementedError()

    def bulk_update(self, objs, fields, batch_size=None):
        raise NotImplementedError()

    if django.VERSION >= (4, 1):

        async def abulk_update(self, objs, fields, batch_size=None):
            raise NotImplementedError()

    def count(self):
        return sum(qs.count() for qs in self._querysets) - self._low_mark

    if django.VERSION >= (4, 1):

        async def acount(self):
            raise NotImplementedError()

    def in_bulk(self, id_list=None, *, field_name="pk"):
        raise NotImplementedError()

    if django.VERSION >= (4, 1):

        async def ain_bulk(self, id_list=None, *, field_name="pk"):
            raise NotImplementedError()

    def iterator(self):
        clone = self._clone()
        clone._querysets = [qs.iterator() for qs in self._querysets]
        return clone

    if django.VERSION >= (4, 1):

        async def aiterator(self):
            raise NotImplementedError()

    def _get_latest_by(self):
        """Process get_latest_by Meta on each QuerySet and return the value."""
        # Get each QuerySet's get_latest_by (ignore unset values).
        get_latest_by = map(
            lambda qs: getattr(qs.model._meta, "get_latest_by"), self._querysets
        )
        get_latest_by = set(get_latest_by)

        # Ensure all of them are identical.
        if len(get_latest_by) > 1:
            raise ValueError(
                "earliest() and latest() require 'get_latest_by' in each "
                "model's Meta to be identical."
            )

        # If all the values are None, get_latest_by was not set.
        if not get_latest_by:
            raise ValueError(
                "earliest() and latest() require either fields as positional "
                "arguments or 'get_latest_by' in the model's Meta."
            )

        # Cast to a list and return the value.
        return list(get_latest_by)

    def _get_first_or_last(self, items, order_fields, reverse):
        # Generate a comparator and sort the items.
        comparator = self._iterable_class._generate_comparator(order_fields)
        items = sorted(items, key=functools.cmp_to_key(comparator), reverse=reverse)

        # Return the first one (whether this is first or last is controlled by
        # reverse).
        return items[0]

    def latest(self, *fields):
        # If fields are given, fallback to get_latest_by.
        if not fields:
            fields = self._get_latest_by()

        objs = []
        for qs in self._querysets:
            try:
                objs.append(qs.latest(*fields))
            except ObjectDoesNotExist:
                pass

        # Checked all QuerySets and no object was found.
        if not objs:
            raise self.model.DoesNotExist()

        # Return the latest.
        return self._get_first_or_last(objs, fields, True)

    if django.VERSION >= (4, 1):

        async def alatest(self, *fields):
            raise NotImplementedError()

    def earliest(self, *fields):
        # If fields are given, fallback to get_latest_by.
        if not fields:
            fields = self._get_latest_by()

        objs = []
        for qs in self._querysets:
            try:
                objs.append(qs.earliest(*fields))
            except ObjectDoesNotExist:
                pass

        # Checked all QuerySets and no object was found.
        if not objs:
            raise self.model.DoesNotExist()

        # Return the latest.
        return self._get_first_or_last(objs, fields, False)

    if django.VERSION >= (4, 1):

        async def aearliest(self, *fields):
            raise NotImplementedError()

    def first(self):
        # If there's no QuerySets, return None. If the QuerySets are unordered,
        # use the first item of first QuerySet. If ordered, compare the first
        # item of each QuerySet to find the overall first.
        if not self._querysets:
            return None

        elif not self.ordered:
            return self._querysets[0].first()

        else:
            # Get each first item for each and compare them, return the "first".
            return self._get_first_or_last(
                [qs.first() for qs in self._querysets], self._order_by, False
            )

    if django.VERSION >= (4, 1):

        async def afirst(self):
            raise NotImplementedError()

    def last(self):
        # See the comments for first().
        if not self._querysets:
            return None

        elif not self.ordered:
            return self._querysets[-1].last()

        else:
            # Get each last item for each and compare them, return the "last".
            return self._get_first_or_last(
                [qs.last() for qs in self._querysets], self._order_by, True
            )

    if django.VERSION >= (4, 1):

        async def alast(self):
            raise NotImplementedError()

    def aggregate(self, *args, **kwargs):
        raise NotImplementedError()

    if django.VERSION >= (4, 1):

        async def aaggregate(self, *args, **kwargs):
            raise NotImplementedError()

    def exists(self):
        return any(qs.exists() for qs in self._querysets)

    if django.VERSION >= (4, 1):

        async def aexists(self):
            raise NotImplementedError()

    def contains(self, obj):
        return any(qs.contains(obj) for qs in self._querysets)

    if django.VERSION >= (4, 1):

        async def acontains(self):
            raise NotImplementedError()

    def update(self, **kwargs):
        with transaction.atomic():
            return sum(qs.update(**kwargs) for qs in self._querysets)

    if django.VERSION >= (4, 1):

        async def aupdate(self, **kwargs):
            raise NotImplementedError()

    def delete(self):
        deleted_count = 0
        deleted_objects = defaultdict(int)
        for qs in self._querysets:
            # Delete this QuerySet.
            current_deleted_count, current_deleted_objects = qs.delete()

            # Combine the results.
            deleted_count += current_deleted_count
            for obj, count in current_deleted_objects.items():
                deleted_objects[obj] += count

        return deleted_count, dict(deleted_objects)

    if django.VERSION >= (4, 1):

        async def adelete(self):
            raise NotImplementedError()

    def as_manager(self):
        raise NotImplementedError()

    def explain(self, format=None, **options):
        return "\n".join(qs.explain(format=format, **options) for qs in self._querysets)

    if django.VERSION >= (4, 1):

        async def aexplain(self, format=None, **options):
            raise NotImplementedError()

    # Public attributes
    @property
    def ordered(self):
        """
        Returns True if the QuerySet is ordered -- i.e. has an order_by()
        clause.
        """
        return bool(self._order_by)

    # Methods specific to QuerySetSequence.
    def get_querysets(self):
        """Returns a list of the QuerySet objects which form the sequence."""
        return self._querysets
