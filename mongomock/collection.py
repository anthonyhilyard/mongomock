from __future__ import division
import bisect
from collections import OrderedDict
try:
	from collections.abc import Mapping, MutableMapping, Iterable
except ImportError:
	from collections import Mapping, MutableMapping, Iterable
import copy
from datetime import datetime
import functools
import itertools
import json
import math
import random
import threading
import time
import types
import warnings

try:
    from bson import json_util, SON, BSON
except ImportError:
    json_utils = SON = BSON = None
try:
    import execjs
except ImportError:
    execjs = None

try:
    from pymongo import ReadPreference
    from pymongo import ReturnDocument

    _READ_PREFERENCE_PRIMARY = ReadPreference.PRIMARY
except ImportError:

    class ReturnDocument(object):
        BEFORE = False
        AFTER = True

    _READ_PREFERENCE_PRIMARY = None

from sentinels import NOTHING
from six import iteritems
from six import iterkeys
from six import itervalues
from six import MAXSIZE
from six import string_types
from six import text_type


from mongomock import aggregate
from mongomock.command_cursor import CommandCursor
from mongomock import ConfigurationError, DuplicateKeyError, BulkWriteError
from mongomock.filtering import filter_applies
from mongomock.filtering import iter_key_candidates
from mongomock.filtering import resolve_sort_key
from mongomock import helpers
from mongomock import InvalidOperation
from mongomock import ObjectId
from mongomock import OperationFailure
from mongomock.results import BulkWriteResult
from mongomock.results import DeleteResult
from mongomock.results import InsertManyResult
from mongomock.results import InsertOneResult
from mongomock.results import UpdateResult
from mongomock.write_concern import WriteConcern
from mongomock import WriteError

lock = threading.RLock()
_random = random.Random()


def validate_is_mapping(option, value):
    if not isinstance(value, Mapping):
        raise TypeError(
            "%s must be an instance of dict, bson.son.SON, or "
            "other type that inherits from "
            "collections.Mapping" % (option,)
        )


def validate_is_mutable_mapping(option, value):
    if not isinstance(value, MutableMapping):
        raise TypeError(
            "%s must be an instance of dict, bson.son.SON, or "
            "other type that inherits from "
            "collections.MutableMapping" % (option,)
        )


def validate_ok_for_replace(replacement):
    validate_is_mapping("replacement", replacement)
    if replacement:
        first = next(iter(replacement))
        if first.startswith("$"):
            raise ValueError("replacement can not include $ operators")


def validate_ok_for_update(update):
    validate_is_mapping("update", update)
    if not update:
        raise ValueError("update only works with $ operators")
    first = next(iter(update))
    if not first.startswith("$"):
        raise ValueError("update only works with $ operators")


def validate_write_concern_params(**params):
    if params:
        WriteConcern(**params)


def get_value_by_dot(doc, key):
    """Get dictionary value using dotted key"""
    result = doc
    for key_item in key.split("."):
        if isinstance(result, dict):
            result = result[key_item]

        elif isinstance(result, (list, tuple)):
            try:
                result = result[int(key_item)]
            except (ValueError, IndexError):
                raise KeyError()

        else:
            raise KeyError()

    return result


def set_value_by_dot(doc, key, value):
    """Set dictionary value using dotted key"""
    try:
        parent_key, child_key = key.rsplit(".", 1)
        parent = get_value_by_dot(doc, parent_key)
    except ValueError:
        child_key = key
        parent = doc

    if isinstance(parent, dict):
        parent[child_key] = value
    elif isinstance(parent, (list, tuple)):
        try:
            parent[int(child_key)] = value
        except (ValueError, IndexError):
            raise KeyError()
    else:
        raise KeyError()

    return doc


def delete_value_by_dot(doc, key):
    """Delete dictionary value using dotted key"""
    try:
        parent_key, child_key = key.rsplit(".", 1)
        parent = get_value_by_dot(doc, parent_key)
    except ValueError:
        child_key = key
        parent = doc

    if isinstance(parent, dict):
        del parent[child_key]
    else:
        raise KeyError()

    return doc


class BulkWriteOperation(object):
    def __init__(self, builder, selector, is_upsert=False):
        self.builder = builder
        self.selector = selector
        self.is_upsert = is_upsert

    def upsert(self):
        assert not self.is_upsert
        return BulkWriteOperation(self.builder, self.selector, is_upsert=True)

    def register_remove_op(self, multi):
        collection = self.builder.collection
        selector = self.selector

        def exec_remove():
            op_result = collection.remove(selector, multi=multi)
            if op_result.get("ok"):
                return {"nRemoved": op_result.get("n")}
            err = op_result.get("err")
            if err:
                return {"writeErrors": [err]}
            return {}

        self.builder.executors.append(exec_remove)

    def remove(self):
        assert not self.is_upsert
        self.register_remove_op(multi=True)

    def remove_one(self,):
        assert not self.is_upsert
        self.register_remove_op(multi=False)

    def register_update_op(self, document, multi, **extra_args):
        if not extra_args.get("remove"):
            validate_ok_for_update(document)

        collection = self.builder.collection
        selector = self.selector

        def exec_update():
            result = collection._update(
                spec=selector,
                document=document,
                multi=multi,
                upsert=self.is_upsert,
                **extra_args
            )
            ret_val = {}
            if result.get("upserted"):
                ret_val["upserted"] = result.get("upserted")
                ret_val["nUpserted"] = result.get("n")
            modified = result.get("nModified")
            if modified is not None:
                ret_val["nModified"] = modified
                ret_val["nMatched"] = modified
            if result.get("err"):
                ret_val["err"] = result.get("err")
            return ret_val

        self.builder.executors.append(exec_update)

    def update(self, document):
        self.register_update_op(document, multi=True)

    def update_one(self, document):
        self.register_update_op(document, multi=False)

    def replace_one(self, document):
        self.register_update_op(document, multi=False, remove=True)


def _combine_projection_spec(projection_fields_spec):
    """Re-format a projection fields spec into a nested dictionary.

    e.g: {'a': 1, 'b.c': 1, 'b.d': 1} => {'a': 1, 'b': {'c': 1, 'd': 1}}
    """

    tmp_spec = OrderedDict()
    for f, v in iteritems(projection_fields_spec):
        if "." not in f:
            if isinstance(tmp_spec.get(f), dict) and not v:
                raise NotImplementedError(
                    "Mongomock does not support overriding excluding projection: %s"
                    % projection_fields_spec
                )
            tmp_spec[f] = v
        else:
            split_field = f.split(".", 1)
            base_field, new_field = tuple(split_field)
            if not isinstance(tmp_spec.get(base_field), dict):
                tmp_spec[base_field] = OrderedDict()
            tmp_spec[base_field][new_field] = v

    combined_spec = OrderedDict()
    for f, v in iteritems(tmp_spec):
        if isinstance(v, dict):
            combined_spec[f] = _combine_projection_spec(v)
        else:
            combined_spec[f] = v

    return combined_spec


def _project_by_spec(doc, combined_projection_spec, is_include, container):
    doc_copy = container()

    if not is_include:
        for key, val in iteritems(doc):
            doc_copy[key] = val

    for key, spec in iteritems(combined_projection_spec):
        if key == '$':
            if is_include:
                raise NotImplementedError('Positional projection is not implemented in mongomock')
            raise OperationFailure('Cannot exclude array elements with the positional operator')
        if key not in doc:
            continue

        if isinstance(spec, dict):
            sub = doc[key]
            if isinstance(sub, (list, tuple)):
                doc_copy[key] = [
                    _project_by_spec(sub_doc, spec, is_include, container)
                    for sub_doc in sub
                ]
            elif isinstance(sub, dict):
                doc_copy[key] = _project_by_spec(sub, spec, is_include, container)
        else:
            if is_include:
                doc_copy[key] = doc[key]
            else:
                doc_copy.pop(key, None)

    return doc_copy


class BulkOperationBuilder(object):
    def __init__(self, collection, ordered=False):
        self.collection = collection
        self.ordered = ordered
        self.results = {}
        self.executors = []
        self.done = False
        self._insert_returns_nModified = True
        self._update_returns_nModified = True

    def find(self, selector):
        return BulkWriteOperation(self, selector)

    def insert(self, doc):
        def exec_insert():
            self.collection.insert(doc)
            return {"nInserted": 1}

        self.executors.append(exec_insert)

    def __aggregate_operation_result(self, total_result, key, value):
        agg_val = total_result.get(key)
        assert (
            agg_val is not None
        ), "Unknow operation result %s=%s" " (unrecognized key)" % (key, value)
        if isinstance(agg_val, int):
            total_result[key] += value
        elif isinstance(agg_val, list):
            if key == "upserted":
                new_element = {"index": len(agg_val), "_id": value}
                agg_val.append(new_element)
            else:
                agg_val.append(value)
        else:
            assert False, (
                "Fixme: missed aggreation rule for type: %s for"
                " key {%s=%s}" % (type(agg_val), key, agg_val)
            )

    def _set_nModified_policy(self, insert, update):
        self._insert_returns_nModified = insert
        self._update_returns_nModified = update

    def execute(self, write_concern=None):
        if not self.executors:
            raise InvalidOperation("Bulk operation empty!")
        if self.done:
            raise InvalidOperation("Bulk operation already executed!")
        self.done = True
        result = {
            "nModified": 0,
            "nUpserted": 0,
            "nMatched": 0,
            "writeErrors": [],
            "upserted": [],
            "writeConcernErrors": [],
            "nRemoved": 0,
            "nInserted": 0,
        }

        has_update = False
        has_insert = False
        broken_nModified_info = False
        for execute_func in self.executors:
            exec_name = execute_func.__name__
            op_result = execute_func()
            for (key, value) in op_result.items():
                self.__aggregate_operation_result(result, key, value)
            if exec_name == "exec_update":
                has_update = True
                if "nModified" not in op_result:
                    broken_nModified_info = True
            has_insert |= exec_name == "exec_insert"

        if broken_nModified_info:
            result.pop("nModified")
        elif has_insert and self._insert_returns_nModified:
            pass
        elif has_update and self._update_returns_nModified:
            pass
        elif self._update_returns_nModified and self._insert_returns_nModified:
            pass
        else:
            result.pop("nModified")
        return result

    def add_insert(self, doc):
        self.insert(doc)

    def add_update(
        self, selector, doc, multi, upsert, collation=None, array_filters=None
    ):
        if array_filters:
            raise NotImplementedError(
                "Array filters are not implemented in mongomock yet."
            )
        write_operation = BulkWriteOperation(self, selector, is_upsert=upsert)
        write_operation.register_update_op(doc, multi)

    def add_replace(self, selector, doc, upsert, collation=None):
        write_operation = BulkWriteOperation(self, selector, is_upsert=upsert)
        write_operation.replace_one(doc)

    def add_delete(self, selector, just_one, collation=None):
        write_operation = BulkWriteOperation(self, selector, is_upsert=False)
        write_operation.register_remove_op(not just_one)


class Collection(object):
    def __init__(self, database, name, create=False, write_concern=None):
        self.name = name
        self.full_name = "{0}.{1}".format(database.name, name)
        self.database = database
        self._documents = OrderedDict()
        self._force_created = create
        self._write_concern = write_concern or WriteConcern()
        self._uniques = {}
        self._index_information = {
            "_id_": {"v": 1, "key": [("_id", 1)], "ns": self.full_name}
        }

    def _is_created(self):
        return self._documents or self._uniques or self._force_created

    def __repr__(self):
        return "Collection({0}, '{1}')".format(self.database, self.name)

    def __getitem__(self, name):
        return self.database[self.name + "." + name]

    def __getattr__(self, name):
        return self.__getitem__(name)

    @property
    def write_concern(self):
        return self._write_concern

    def initialize_unordered_bulk_op(self):
        return BulkOperationBuilder(self, ordered=False)

    def initialize_ordered_bulk_op(self):
        return BulkOperationBuilder(self, ordered=True)

    def insert(
        self, data, manipulate=True, check_keys=True, continue_on_error=False, **kwargs
    ):
        warnings.warn(
            "insert is deprecated. Use insert_one or insert_many " "instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        validate_write_concern_params(**kwargs)
        return self._insert(data)

    def insert_one(self, document, session=None):
        validate_is_mutable_mapping("document", document)
        return InsertOneResult(self._insert(document, session), acknowledged=True)

    def insert_many(self, documents, ordered=True, session=None):
        if not isinstance(documents, Iterable) or not documents:
            raise TypeError("documents must be a non-empty list")
        for document in documents:
            validate_is_mutable_mapping("document", document)
        return InsertManyResult(self._insert(documents, session), acknowledged=True)

    def _insert(self, data, session=None):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        if isinstance(data, list) or isinstance(data, types.GeneratorType):
            results = []
            for index, item in enumerate(data):
                try:
                    results.append(self._insert(item))
                except DuplicateKeyError:
                    raise BulkWriteError(
                        {
                            "writeErrors": [
                                {
                                    "index": index,
                                    "code": 11000,
                                    "errmsg": "E11000 duplicate key error",
                                    "op": item,
                                }
                            ],
                            "nInserted": index,
                        }
                    )
            return results

        # Like pymongo, we should fill the _id in the inserted dict (odd behavior,
        # but we need to stick to it), so we must patch in-place the data dict
        for key in data.keys():
            data[key] = helpers.patch_datetime_awareness_in_document(data[key])

        if not all(isinstance(k, string_types) for k in data):
            raise ValueError("Document keys must be strings")

        if BSON:
            # bson validation
            BSON.encode(data, check_keys=True)

        if "_id" not in data:
            data["_id"] = ObjectId()
        object_id = data["_id"]
        if isinstance(object_id, dict):
            object_id = helpers.hashdict(object_id)
        if object_id in self._documents:
            raise DuplicateKeyError("E11000 Duplicate Key Error", 11000)
        with lock:
            self._documents[object_id] = self._internalize_dict(data)
        try:
            self._ensure_uniques(data)
        except DuplicateKeyError:
            # Rollback
            del self._documents[object_id]
            raise
        return data["_id"]

    def _ensure_uniques(self, new_data):
        # Note we consider new_data is already inserted in db
        for unique, is_sparse in self._uniques.values():
            find_kwargs = {}
            for key, direction in unique:
                try:
                    find_kwargs[key] = get_value_by_dot(new_data, key)
                except KeyError:
                    find_kwargs[key] = None
            answer_count = len(list(self._iter_documents(find_kwargs)))
            if answer_count > 1 and not (is_sparse and find_kwargs[key] is None):
                raise DuplicateKeyError("E11000 Duplicate Key Error", 11000)

    def _internalize_dict(self, d):
        return {k: copy.deepcopy(v) for k, v in iteritems(d)}

    def _has_key(self, doc, key):
        key_parts = key.split(".")
        sub_doc = doc
        for part in key_parts:
            if part not in sub_doc:
                return False
            sub_doc = sub_doc[part]
        return True

    def _remove_key(self, doc, key):
        key_parts = key.split(".")
        sub_doc = doc
        for part in key_parts[:-1]:
            sub_doc = sub_doc[part]
        del sub_doc[key_parts[-1]]

    def update_one(self, filter, update, upsert=False, session=None):
        validate_ok_for_update(update)
        return UpdateResult(
            self._update(filter, update, upsert=upsert, session=session),
            acknowledged=True,
        )

    def update_many(self, filter, update, upsert=False, session=None):
        validate_ok_for_update(update)
        return UpdateResult(
            self._update(filter, update, upsert=upsert, multi=True, session=session),
            acknowledged=True,
        )

    def replace_one(self, filter, replacement, upsert=False, session=None):
        validate_ok_for_replace(replacement)
        return UpdateResult(
            self._update(filter, replacement, upsert=upsert, session=session),
            acknowledged=True,
        )

    def update(
        self,
        spec,
        document,
        upsert=False,
        manipulate=False,
        multi=False,
        check_keys=False,
        **kwargs
    ):
        warnings.warn(
            "update is deprecated. Use replace_one, update_one or "
            "update_many instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._update(
            spec, document, upsert, manipulate, multi, check_keys, **kwargs
        )

    def _update(
        self,
        spec,
        document,
        upsert=False,
        manipulate=False,
        multi=False,
        check_keys=False,
        session=None,
        **kwargs
    ):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        spec = helpers.patch_datetime_awareness_in_document(spec)
        document = helpers.patch_datetime_awareness_in_document(document)
        validate_is_mapping("spec", spec)
        validate_is_mapping("document", document)

        updated_existing = False
        upserted_id = None
        num_updated = 0
        for existing_document in itertools.chain(self._iter_documents(spec), [None]):
            # we need was_insert for the setOnInsert update operation
            was_insert = False
            # the sentinel document means we should do an upsert
            if existing_document is None:
                if not upsert or num_updated:
                    continue
                # For upsert operation we have first to create a fake existing_document,
                # update it like a regular one, then finally insert it
                if spec.get("_id") is not None:
                    _id = spec["_id"]
                elif document.get("_id") is not None:
                    _id = document["_id"]
                else:
                    _id = ObjectId()
                to_insert = dict(spec, _id=_id)
                to_insert = self._expand_dots(to_insert)
                existing_document = to_insert
                was_insert = True
            else:
                original_document_snapshot = copy.deepcopy(existing_document)
                updated_existing = True
            num_updated += 1
            first = True
            subdocument = None
            for k, v in iteritems(document):
                if k in _updaters.keys():
                    updater = _updaters[k]
                    subdocument = self._update_document_fields_with_positional_awareness(
                        existing_document, v, spec, updater, subdocument
                    )

                elif k == "$rename":
                    for src, dst in iteritems(v):
                        if "." in src or "." in dst:
                            raise NotImplementedError(
                                "Using the $rename operator with dots is a valid MongoDB "
                                "operation, but it is not yet supported by mongomock"
                            )
                        if self._has_key(existing_document, src):
                            existing_document[dst] = existing_document.pop(src)

                elif k == "$setOnInsert":
                    if not was_insert:
                        continue
                    subdocument = self._update_document_fields_with_positional_awareness(
                        existing_document, v, spec, _set_updater, subdocument
                    )

                elif k == "$currentDate":
                    for value in itervalues(v):
                        if value == {"$type": "timestamp"}:
                            raise NotImplementedError(
                                "timestamp is not supported so far"
                            )

                    subdocument = self._update_document_fields_with_positional_awareness(
                        existing_document, v, spec, _current_date_updater, subdocument
                    )

                elif k == "$addToSet":
                    for field, value in iteritems(v):
                        nested_field_list = field.rsplit(".")
                        if len(nested_field_list) == 1:
                            if field not in existing_document:
                                existing_document[field] = []
                            # document should be a list append to it
                            if isinstance(value, dict):
                                if "$each" in value:
                                    # append the list to the field
                                    existing_document[field] += [
                                        obj
                                        for obj in list(value["$each"])
                                        if obj not in existing_document[field]
                                    ]
                                    continue
                            if value not in existing_document[field]:
                                existing_document[field].append(value)
                            continue
                        # push to array in a nested attribute
                        else:
                            # create nested attributes if they do not exist
                            subdocument = existing_document
                            for field in nested_field_list[:-1]:
                                if field not in subdocument:
                                    subdocument[field] = {}

                                subdocument = subdocument[field]

                            # we're pushing a list
                            push_results = []
                            if nested_field_list[-1] in subdocument:
                                # if the list exists, then use that list
                                push_results = subdocument[nested_field_list[-1]]

                            if isinstance(value, dict) and "$each" in value:
                                push_results += [
                                    obj
                                    for obj in list(value["$each"])
                                    if obj not in push_results
                                ]
                            elif value not in push_results:
                                push_results.append(value)

                            subdocument[nested_field_list[-1]] = push_results
                elif k == "$pull":
                    for field, value in iteritems(v):
                        nested_field_list = field.rsplit(".")
                        # nested fields includes a positional element
                        # need to find that element
                        if "$" in nested_field_list:
                            if not subdocument:
                                subdocument = self._get_subdocument(
                                    existing_document, spec, nested_field_list
                                )

                            # value should be a dictionary since we're pulling
                            pull_results = []
                            # and the last subdoc should be an array
                            for obj in subdocument[nested_field_list[-1]]:
                                if isinstance(obj, dict):
                                    for pull_key, pull_value in iteritems(value):
                                        if obj[pull_key] != pull_value:
                                            pull_results.append(obj)
                                    continue
                                if obj != value:
                                    pull_results.append(obj)

                            # cannot write to doc directly as it doesn't save to
                            # existing_document
                            subdocument[nested_field_list[-1]] = pull_results
                        else:
                            arr = existing_document
                            for field in nested_field_list:
                                if field not in arr:
                                    break
                                arr = arr[field]
                            if not isinstance(arr, list):
                                continue

                            arr_copy = copy.deepcopy(arr)
                            if isinstance(value, dict):
                                for obj in arr_copy:
                                    if filter_applies(value, obj):
                                        arr.remove(obj)
                            else:
                                for obj in arr_copy:
                                    if value == obj:
                                        arr.remove(obj)
                elif k == "$pullAll":
                    for field, value in iteritems(v):
                        nested_field_list = field.rsplit(".")
                        if len(nested_field_list) == 1:
                            if field in existing_document:
                                arr = existing_document[field]
                                existing_document[field] = [
                                    obj for obj in arr if obj not in value
                                ]
                            continue
                        else:
                            subdocument = existing_document
                            for nested_field in nested_field_list[:-1]:
                                if nested_field not in subdocument:
                                    break
                                subdocument = subdocument[nested_field]

                            if nested_field_list[-1] in subdocument:
                                arr = subdocument[nested_field_list[-1]]
                                subdocument[nested_field_list[-1]] = [
                                    obj for obj in arr if obj not in value
                                ]
                elif k == "$push":
                    for field, value in iteritems(v):
                        nested_field_list = field.rsplit(".")
                        if len(nested_field_list) == 1:
                            if field not in existing_document:
                                existing_document[field] = []
                            # document should be a list
                            # append to it
                            if isinstance(value, dict):
                                if "$slice" in value:
                                    raise NotImplementedError(
                                        "$slice is a valid modifier of a $push operation but it is "
                                        "not supported by Mongomock yet"
                                    )
                                if "$each" in value:
                                    # append the list to the field
                                    existing_document[field] += list(value["$each"])
                                    continue
                            existing_document[field].append(value)
                            continue
                        # nested fields includes a positional element
                        # need to find that element
                        elif "$" in nested_field_list:
                            if not subdocument:
                                subdocument = self._get_subdocument(
                                    existing_document, spec, nested_field_list
                                )

                            # we're pushing a list
                            push_results = []
                            if nested_field_list[-1] in subdocument:
                                # if the list exists, then use that list
                                push_results = subdocument[nested_field_list[-1]]

                            if isinstance(value, dict):
                                if "$slice" in value:
                                    raise NotImplementedError(
                                        "$slice is a valid modifier of a $push operation but it is "
                                        "not supported by Mongomock yet"
                                    )
                                # check to see if we have the format
                                # { '$each': [] }
                                if "$each" in value:
                                    push_results += list(value["$each"])
                                else:
                                    push_results.append(value)
                            else:
                                push_results.append(value)

                            # cannot write to doc directly as it doesn't save to
                            # existing_document
                            subdocument[nested_field_list[-1]] = push_results
                        # push to array in a nested attribute
                        else:
                            # create nested attributes if they do not exist
                            subdocument = existing_document
                            for field in nested_field_list[:-1]:
                                if isinstance(subdocument, dict):
                                    if field not in subdocument:
                                        subdocument[field] = {}
                                    subdocument = subdocument[field]
                                else:
                                    subdocument = subdocument[int(field)]

                            # we're pushing a list
                            push_results = []
                            if nested_field_list[-1] in subdocument:
                                # if the list exists, then use that list
                                push_results = subdocument[nested_field_list[-1]]

                            if isinstance(value, dict) and "$each" in value:
                                if "$slice" in value:
                                    raise NotImplementedError(
                                        "$slice is a valid modifier of a $push operation but it is "
                                        "not supported by Mongomock yet"
                                    )
                                push_results += list(value["$each"])
                            else:
                                push_results.append(value)

                            subdocument[nested_field_list[-1]] = push_results
                else:
                    if first:
                        # replace entire document
                        for key in document.keys():
                            if key.startswith("$"):
                                # can't mix modifiers with non-modifiers in
                                # update
                                raise ValueError(
                                    "field names cannot start with $ [{}]".format(k)
                                )
                        _id = spec.get("_id", existing_document.get("_id"))
                        existing_document.clear()
                        if _id:
                            existing_document["_id"] = _id
                        if BSON:
                            # bson validation
                            BSON.encode(document, check_keys=True)
                        existing_document.update(self._internalize_dict(document))
                        if existing_document["_id"] != _id:
                            raise OperationFailure(
                                "The _id field cannot be changed from {0} to {1}".format(
                                    existing_document["_id"], _id
                                )
                            )
                        break
                    else:
                        # can't mix modifiers with non-modifiers in update
                        raise ValueError("Invalid modifier specified: {}".format(k))
                first = False
            # if empty document comes
            if not document:
                _id = spec.get("_id", existing_document.get("_id"))
                existing_document.clear()
                if _id:
                    existing_document["_id"] = _id

            if was_insert:
                upserted_id = self._insert(existing_document)
            else:
                # Document has been modified in-place, last thing is to make sure it
                # still respect the unique indexes and if not to revert modifications
                try:
                    self._ensure_uniques(existing_document)
                except DuplicateKeyError:
                    # Rollback
                    self._documents[
                        original_document_snapshot["_id"]
                    ] = original_document_snapshot
                    raise

            if not multi:
                break

        return {
            text_type("connectionId"): self.database.client._id,
            text_type("err"): None,
            text_type("n"): num_updated,
            text_type("nModified"): num_updated if updated_existing else 0,
            text_type("ok"): 1,
            text_type("upserted"): upserted_id,
            text_type("updatedExisting"): updated_existing,
        }

    def _get_subdocument(self, existing_document, spec, nested_field_list):
        """This method retrieves the subdocument of the existing_document.nested_field_list.

        It uses the spec to filter through the items. It will continue to grab nested documents
        until it can go no further. It will then return the subdocument that was last saved.
        '$' is the positional operator, so we use the $elemMatch in the spec to find the right
        subdocument in the array.
        """
        # current document in view
        doc = existing_document
        # previous document in view
        subdocument = existing_document
        # current spec in view
        subspec = spec
        # walk down the dictionary
        for subfield in nested_field_list:
            if subfield == "$":
                # positional element should have the equivalent elemMatch in the
                # query
                subspec = subspec["$elemMatch"]
                for item in doc:
                    # iterate through
                    if filter_applies(subspec, item):
                        # found the matching item save the parent
                        subdocument = doc
                        # save the item
                        doc = item
                        break
                continue

            subdocument = doc
            doc = doc[subfield]
            if subfield not in subspec:
                break
            subspec = subspec[subfield]

        return subdocument

    def _expand_dots(self, doc):
        expanded = {}
        paths = {}
        for k, v in iteritems(doc):
            key_parts = k.split(".")
            sub_doc = v
            for i in reversed(range(1, len(key_parts))):
                key = key_parts[i]
                sub_doc = {key: sub_doc}
            key = key_parts[0]
            if key in expanded:
                raise WriteError(
                    "cannot infer query fields to set, "
                    "both paths '%s' and '%s' are matched" % (k, paths[key])
                )
            paths[key] = k
            expanded[key] = sub_doc
        return expanded

    def _discard_operators(self, doc):
        # TODO(this looks a little too naive...)
        return {k: v for k, v in iteritems(doc) if not k.startswith("$")}

    def find(
        self,
        filter=None,
        projection=None,
        skip=0,
        limit=0,
        no_cursor_timeout=False,
        cursor_type=None,
        sort=None,
        allow_partial_results=False,
        oplog_replay=False,
        modifiers=None,
        batch_size=0,
        manipulate=True,
        collation=None,
        session=None,
    ):
        spec = filter
        if spec is None:
            spec = {}
        validate_is_mapping("filter", spec)
        return Cursor(self, spec, sort, projection, skip, limit, collation=collation)

    def _get_dataset(self, spec, sort, fields, as_class):
        dataset = self._iter_documents(spec)
        if sort:
            for sort_key, sort_direction in reversed(sort):
                if sort_key == '$natural':
                    if sort_direction < 0:
                        dataset = iter(reversed(list(dataset)))
                    continue
                dataset = iter(sorted(
                    dataset, key=lambda x: resolve_sort_key(sort_key, x),
                    reverse=sort_direction < 0))
        for document in dataset:
            yield self._copy_only_fields(document, fields, as_class)

    def _copy_field(self, obj, container):
        if isinstance(obj, list):
            new = []
            for item in obj:
                new.append(self._copy_field(item, container))
            return new
        if isinstance(obj, dict):
            new = container()
            for key, value in obj.items():
                new[key] = self._copy_field(value, container)
            return new
        return copy.copy(obj)

    def _extract_projection_operators(self, fields):
        """Removes and returns fields with projection operators."""
        result = {}
        allowed_projection_operators = {"$elemMatch"}
        for key, value in iteritems(fields):
            if isinstance(value, dict):
                for op in value:
                    if op not in allowed_projection_operators:
                        raise ValueError("Unsupported projection option: {}".format(op))
                result[key] = value

        for key in result:
            del fields[key]

        return result

    def _apply_projection_operators(self, ops, doc, doc_copy):
        """Applies projection operators to copied document."""
        for field, op in iteritems(ops):
            if field not in doc_copy:
                if field in doc:
                    # field was not copied yet (since we are in include mode)
                    doc_copy[field] = doc[field]
                else:
                    # field doesn't exist in original document, no work to do
                    continue

            if "$elemMatch" in op:
                if isinstance(doc_copy[field], list):
                    # find the first item that matches
                    matched = False
                    for item in doc_copy[field]:
                        if filter_applies(op["$elemMatch"], item):
                            matched = True
                            doc_copy[field] = [item]
                            break

                    # nothing have matched
                    if not matched:
                        del doc_copy[field]

                else:
                    # remove the field since there is nothing to iterate
                    del doc_copy[field]

    def _copy_only_fields(self, doc, fields, container):
        """Copy only the specified fields."""

        if fields is None:
            return self._copy_field(doc, container)

        if not fields:
            fields = {"_id": 1}
        if not isinstance(fields, dict):
            fields = helpers._fields_list_to_dict(fields)

        # we can pass in something like {'_id':0, 'field':1}, so pull the id
        # value out and hang on to it until later
        id_value = fields.pop("_id", 1)

        # filter out fields with projection operators, we will take care of them later
        projection_operators = self._extract_projection_operators(fields)

        # other than the _id field, all fields must be either includes or
        # excludes, this can evaluate to 0
        if len(set(list(fields.values()))) > 1:
            raise ValueError("You cannot currently mix including and excluding fields.")

        # if we have novalues passed in, make a doc_copy based on the
        # id_value
        if not fields:
            if id_value == 1:
                doc_copy = container()
            else:
                doc_copy = self._copy_field(doc, container)
        else:
            doc_copy = _project_by_spec(
                doc,
                _combine_projection_spec(fields),
                is_include=list(fields.values())[0],
                container=container,
            )

        # set the _id value if we requested it, otherwise remove it
        if id_value == 0:
            doc_copy.pop("_id", None)
        else:
            if "_id" in doc:
                doc_copy["_id"] = doc["_id"]

        fields["_id"] = id_value  # put _id back in fields

        # time to apply the projection operators and put back their fields
        self._apply_projection_operators(projection_operators, doc, doc_copy)
        for field, op in iteritems(projection_operators):
            fields[field] = op
        return doc_copy

    def _update_document_fields(self, doc, fields, updater):
        """Implements the $set behavior on an existing document"""
        for k, v in iteritems(fields):
            self._update_document_single_field(doc, k, v, updater)

    def _update_document_fields_positional(
        self, doc, fields, spec, updater, subdocument=None
    ):
        """Implements the $set behavior on an existing document"""
        for k, v in iteritems(fields):
            if "$" in k:

                field_name_parts = k.split(".")
                if not subdocument:
                    current_doc = doc
                    subspec = spec
                    for part in field_name_parts[:-1]:
                        if part == "$":
                            subspec = subspec.get("$elemMatch", subspec)
                            for item in current_doc:
                                if filter_applies(subspec, item):
                                    current_doc = item
                                    break
                            continue

                        new_spec = {}
                        for el in subspec:
                            if el.startswith(part):
                                if len(el.split(".")) > 1:
                                    new_spec[".".join(el.split(".")[1:])] = subspec[el]
                                else:
                                    new_spec = subspec[el]
                        subspec = new_spec
                        current_doc = current_doc[part]

                    subdocument = current_doc
                    if field_name_parts[-1] == "$" and isinstance(subdocument, list):
                        for i, doc in enumerate(subdocument):
                            if filter_applies(subspec, doc):
                                subdocument[i] = v
                                break
                        continue

                updater(subdocument, field_name_parts[-1], v)
                continue
            # otherwise, we handle it the standard way
            self._update_document_single_field(doc, k, v, updater)

        return subdocument

    def _update_document_fields_with_positional_awareness(
        self, existing_document, v, spec, updater, subdocument
    ):
        positional = any("$" in key for key in iterkeys(v))

        if positional:
            return self._update_document_fields_positional(
                existing_document, v, spec, updater, subdocument
            )
        self._update_document_fields(existing_document, v, updater)
        return subdocument

    def _update_document_single_field(self, doc, field_name, field_value, updater):
        field_name_parts = field_name.split(".")
        for part in field_name_parts[:-1]:
            if isinstance(doc, list):
                try:
                    if part == "$":
                        doc = doc[0]
                    else:
                        doc = doc[int(part)]
                    continue
                except ValueError:
                    pass
            elif isinstance(doc, dict):
                if updater is _unset_updater and part not in doc:
                    # If the parent doesn't exists, so does it child.
                    return
                doc = doc.setdefault(part, {})
            else:
                return
        field_name = field_name_parts[-1]
        if isinstance(doc, list):
            try:
                doc[int(field_name)] = field_value
            except IndexError:
                pass
        else:
            updater(doc, field_name, field_value)

    def _iter_documents(self, filter=None):
        return (
            document
            for document in list(itervalues(self._documents))
            if filter_applies(filter, document)
        )

    def find_one(self, filter=None, *args, **kwargs):
        # Allow calling find_one with a non-dict argument that gets used as
        # the id for the query.
        if filter is None:
            filter = {}
        if not isinstance(filter, Mapping):
            filter = {"_id": filter}

        try:
            return next(self.find(filter, *args, **kwargs))
        except StopIteration:
            return None

    def find_one_and_delete(self, filter, projection=None, sort=None, **kwargs):
        kwargs["remove"] = True
        validate_is_mapping("filter", filter)
        return self._find_and_modify(filter, projection, sort=sort, **kwargs)

    def find_one_and_replace(
        self,
        filter,
        replacement,
        projection=None,
        sort=None,
        upsert=False,
        return_document=ReturnDocument.BEFORE,
        **kwargs
    ):
        validate_is_mapping("filter", filter)
        validate_ok_for_replace(replacement)
        return self._find_and_modify(
            filter, projection, replacement, upsert, sort, return_document, **kwargs
        )

    def find_one_and_update(
        self,
        filter,
        update,
        projection=None,
        sort=None,
        upsert=False,
        return_document=ReturnDocument.BEFORE,
        **kwargs
    ):
        validate_is_mapping("filter", filter)
        validate_ok_for_update(update)
        return self._find_and_modify(
            filter, projection, update, upsert, sort, return_document, **kwargs
        )

    def find_and_modify(
        self,
        query={},
        update=None,
        upsert=False,
        sort=None,
        full_response=False,
        manipulate=False,
        fields=None,
        **kwargs
    ):
        warnings.warn(
            "find_and_modify is deprecated, use find_one_and_delete"
            ", find_one_and_replace, or find_one_and_update instead",
            DeprecationWarning,
            stacklevel=2,
        )
        if "projection" in kwargs:
            raise TypeError(
                "find_and_modify() got an unexpected keyword argument 'projection'"
            )
        return self._find_and_modify(
            query, update=update, upsert=upsert, sort=sort, projection=fields, **kwargs
        )

    def _find_and_modify(
        self,
        query,
        projection=None,
        update=None,
        upsert=False,
        sort=None,
        return_document=ReturnDocument.BEFORE,
        session=None,
        **kwargs
    ):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        remove = kwargs.get("remove", False)
        if kwargs.get("new", False) and remove:
            # message from mongodb
            raise OperationFailure("remove and returnNew can't co-exist")

        if not (remove or update):
            raise ValueError("Must either update or remove")

        if remove and update:
            raise ValueError("Can't do both update and remove")

        old = self.find_one(query, projection=projection, sort=sort)
        if not old and not upsert:
            return

        if old and "_id" in old:
            query = {"_id": old["_id"]}

        if remove:
            self.delete_one(query)
        else:
            self._update(query, update, upsert)

        if return_document is ReturnDocument.AFTER or kwargs.get("new"):
            return self.find_one(query, projection)
        return old

    def save(self, to_save, manipulate=True, check_keys=True, **kwargs):
        warnings.warn(
            "save is deprecated. Use insert_one or replace_one " "instead",
            DeprecationWarning,
            stacklevel=2,
        )
        validate_is_mutable_mapping("to_save", to_save)
        validate_write_concern_params(**kwargs)

        if "_id" not in to_save:
            return self.insert(to_save)
        self._update(
            {"_id": to_save["_id"]},
            to_save,
            True,
            manipulate,
            check_keys=True,
            **kwargs
        )
        return to_save.get("_id", None)

    def delete_one(self, filter, session=None):
        validate_is_mapping("filter", filter)
        return DeleteResult(self._delete(filter, session=session), True)

    def delete_many(self, filter, session=None):
        validate_is_mapping("filter", filter)
        return DeleteResult(self._delete(filter, multi=True, session=session), True)

    def _delete(self, filter, multi=False, session=None):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        filter = helpers.patch_datetime_awareness_in_document(filter)
        if filter is None:
            filter = {}
        if not isinstance(filter, Mapping):
            filter = {"_id": filter}
        to_delete = list(self.find(filter))
        deleted_count = 0
        for doc in to_delete:
            doc_id = doc["_id"]
            if isinstance(doc_id, dict):
                doc_id = helpers.hashdict(doc_id)
            del self._documents[doc_id]
            deleted_count += 1
            if not multi:
                break

        return {
            "connectionId": self.database.client._id,
            "n": deleted_count,
            "ok": 1.0,
            "err": None,
        }

    def remove(self, spec_or_id=None, multi=True, **kwargs):
        warnings.warn(
            "remove is deprecated. Use delete_one or delete_many " "instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        validate_write_concern_params(**kwargs)
        return self._delete(spec_or_id, multi=multi)

    def count(self, filter=None, **kwargs):
        warnings.warn(
            "count is deprecated. Use estimated_document_count or "
            "count_documents instead. Please note that $where must be replaced "
            "by $expr, $near must be replaced by $geoWithin with $center, and "
            "$nearSphere must be replaced by $geoWithin with $centerSphere",
            DeprecationWarning,
            stacklevel=2,
        )
        if kwargs.pop("session", None):
            raise NotImplementedError("Mongomock does not handle sessions yet")
        if filter is None:
            return len(self._documents)
        return len(list(self._iter_documents(filter)))

    def count_documents(self, filter, **kwargs):
        if kwargs.pop("collation", None):
            raise NotImplementedError(
                "The collation argument of count_documents is valid but has not been "
                "implemented in mongomock yet"
            )
        if kwargs.pop("session", None):
            raise NotImplementedError("Mongomock does not handle sessions yet")
        skip = kwargs.pop("skip", 0)
        if "limit" in kwargs:
            limit = kwargs.pop("limit")
            if not isinstance(limit, (int, float)):
                raise OperationFailure("the limit must be specified as a number")
            if limit <= 0:
                raise OperationFailure("the limit must be positive")
            limit = math.floor(limit)
        else:
            limit = None
        unknown_kwargs = set(kwargs) - {"maxTimeMS", "hint"}
        if unknown_kwargs:
            raise OperationFailure("unrecognized field '%s'" % unknown_kwargs.pop())

        count = len(list(self._iter_documents(filter)))
        if limit is None:
            return count - skip
        return min(count - skip, limit)

    def estimated_document_count(self, **kwargs):
        if kwargs.pop("session", None):
            raise ConfigurationError(
                "estimated_document_count does not support sessions"
            )
        # Only some kwargs are recognized by this method, however the others
        # are ignored silently by pymongo.
        fwd_kwargs = {
            k: v
            for k, v in iteritems(kwargs)
            if k in {"skip", "limit", "maxTimeMS", "hint"}
        }
        return self.count_documents({}, **fwd_kwargs)

    def drop(self, session=None):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        self.database.drop_collection(self.name)

    def ensure_index(self, key_or_list, cache_for=300, **kwargs):
        self.create_index(key_or_list, cache_for, **kwargs)

    def create_index(self, key_or_list, cache_for=300, session=None, **kwargs):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        index_list = helpers.index_list(key_or_list)
        is_unique = kwargs.pop("unique", False)
        is_sparse = kwargs.pop("sparse", False)

        index_string = "_".join("_".join([str(i) for i in ix]) for ix in index_list)
        self._index_information[index_string] = {
            "key": index_list,
            "ns": self.full_name,
            "v": 1,
        }

        # Check that documents already verify the uniquess of this new index.
        if is_unique:
            self._index_information[index_string]["unique"] = True
            indexed = set()
            for doc in itervalues(self._documents):
                index = []
                for key, unused_order in index_list:
                    try:
                        index.append(get_value_by_dot(doc, key))
                    except KeyError:
                        if is_sparse:
                            continue
                        index.append(None)
                index = tuple(index)
                if index in indexed:
                    raise DuplicateKeyError("E11000 Duplicate Key Error", 11000)
                indexed.add(index)

            self._uniques[index_string] = (index_list, is_sparse)

        return index_string

    def drop_index(self, index_or_name, session=None):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        self._index_information.pop(index_or_name, None)
        self._uniques.pop(index_or_name, None)

    def drop_indexes(self, session=None):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        self._uniques = {}
        self._index_information = {
            "_id_": {"v": 1, "key": [("_id", 1)], "ns": self.full_name}
        }

    def reindex(self, session=None):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")

    def list_indexes(self, session=None):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        for name, information in self._index_information.items():
            yield dict(information, name=name)

    def index_information(self, session=None):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        return copy.deepcopy(self._index_information)

    def map_reduce(
        self,
        map_func,
        reduce_func,
        out,
        full_response=False,
        query=None,
        limit=0,
        session=None,
    ):
        if execjs is None:
            raise NotImplementedError(
                "PyExecJS is required in order to run Map-Reduce. "
                "Use 'pip install pyexecjs pymongo' to support Map-Reduce mock."
            )
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        if limit == 0:
            limit = None
        start_time = time.clock()
        out_collection = None
        reduced_rows = None
        full_dict = {
            "counts": {"input": 0, "reduce": 0, "emit": 0, "output": 0},
            "timeMillis": 0,
            "ok": 1.0,
            "result": None,
        }
        map_ctx = execjs.compile(
            """
            function doMap(fnc, docList) {
                var mappedDict = {};
                function emit(key, val) {
                    if (key['$oid']) {
                        mapped_key = '$oid' + key['$oid'];
                    }
                    else {
                        mapped_key = key;
                    }
                    if(!mappedDict[mapped_key]) {
                        mappedDict[mapped_key] = [];
                    }
                    mappedDict[mapped_key].push(val);
                }
                mapper = eval('('+fnc+')');
                var mappedList = new Array();
                for(var i=0; i<docList.length; i++) {
                    var thisDoc = eval('('+docList[i]+')');
                    var mappedVal = (mapper).call(thisDoc);
                }
                return mappedDict;
            }
        """
        )
        reduce_ctx = execjs.compile(
            """
            function doReduce(fnc, docList) {
                var reducedList = new Array();
                reducer = eval('('+fnc+')');
                for(var key in docList) {
                    var reducedVal = {'_id': key,
                            'value': reducer(key, docList[key])};
                    reducedList.push(reducedVal);
                }
                return reducedList;
            }
        """
        )
        doc_list = [
            json.dumps(doc, default=json_util.default) for doc in self.find(query)
        ]
        mapped_rows = map_ctx.call("doMap", map_func, doc_list)
        reduced_rows = reduce_ctx.call("doReduce", reduce_func, mapped_rows)[:limit]
        for reduced_row in reduced_rows:
            if reduced_row["_id"].startswith("$oid"):
                reduced_row["_id"] = ObjectId(reduced_row["_id"][4:])
        reduced_rows = sorted(reduced_rows, key=lambda x: x["_id"])
        if full_response:
            full_dict["counts"]["input"] = len(doc_list)
            for key in mapped_rows.keys():
                emit_count = len(mapped_rows[key])
                full_dict["counts"]["emit"] += emit_count
                if emit_count > 1:
                    full_dict["counts"]["reduce"] += 1
            full_dict["counts"]["output"] = len(reduced_rows)
        if isinstance(out, (string_types, bytes)):
            out_collection = getattr(self.database, out)
            out_collection.drop()
            out_collection.insert(reduced_rows)
            ret_val = out_collection
            full_dict["result"] = out
        elif isinstance(out, SON) and out.get("replace") and out.get("db"):
            # Must be of the format SON([('replace','results'),('db','outdb')])
            out_db = getattr(self.database._client, out["db"])
            out_collection = getattr(out_db, out["replace"])
            out_collection.insert(reduced_rows)
            ret_val = out_collection
            full_dict["result"] = {"db": out["db"], "collection": out["replace"]}
        elif isinstance(out, dict) and out.get("inline"):
            ret_val = reduced_rows
            full_dict["result"] = reduced_rows
        else:
            raise TypeError("'out' must be an instance of string, dict or bson.SON")
        full_dict["timeMillis"] = int(round((time.clock() - start_time) * 1000))
        if full_response:
            ret_val = full_dict
        return ret_val

    def inline_map_reduce(
        self,
        map_func,
        reduce_func,
        full_response=False,
        query=None,
        limit=0,
        session=None,
    ):
        return self.map_reduce(
            map_func,
            reduce_func,
            {"inline": 1},
            full_response,
            query,
            limit,
            session=session,
        )

    def distinct(self, key, filter=None, session=None):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        return self.find(filter).distinct(key)

    def group(self, key, condition, initial, reduce, finalize=None):
        if execjs is None:
            raise NotImplementedError(
                "PyExecJS is required in order to use group. "
                "Use 'pip install pyexecjs pymongo' to support group mock."
            )
        reduce_ctx = execjs.compile(
            """
            function doReduce(fnc, docList) {
                reducer = eval('('+fnc+')');
                for(var i=0, l=docList.length; i<l; i++) {
                    try {
                        reducedVal = reducer(docList[i-1], docList[i]);
                    }
                    catch (err) {
                        continue;
                    }
                }
            return docList[docList.length - 1];
            }
        """
        )

        ret_array = []
        doc_list_copy = []
        ret_array_copy = []
        reduced_val = {}
        doc_list = [doc for doc in self.find(condition)]
        for doc in doc_list:
            doc_copy = copy.deepcopy(doc)
            for k in doc:
                if isinstance(doc[k], ObjectId):
                    doc_copy[k] = str(doc[k])
                if k not in key and k not in reduce:
                    del doc_copy[k]
            for initial_key in initial:
                if initial_key in doc.keys():
                    pass
                else:
                    doc_copy[initial_key] = initial[initial_key]
            doc_list_copy.append(doc_copy)
        doc_list = doc_list_copy
        for k in key:
            doc_list = sorted(doc_list, key=lambda x: _resolve_key(k, x))
        for k in key:
            if not isinstance(k, string_types):
                raise TypeError(
                    "Keys must be a list of key names, "
                    "each an instance of %s" % string_types[0].__name__
                )
            for k2, group in itertools.groupby(doc_list, lambda item: item[k]):
                group_list = [x for x in group]
                reduced_val = reduce_ctx.call("doReduce", reduce, group_list)
                ret_array.append(reduced_val)
        for doc in ret_array:
            doc_copy = copy.deepcopy(doc)
            for k in doc:
                if k not in key and k not in initial.keys():
                    del doc_copy[k]
            ret_array_copy.append(doc_copy)
        ret_array = ret_array_copy
        return ret_array

    def aggregate(self, pipeline, session=None, **kwargs):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        pipeline_operators = [
            "$addFields",
            "$bucket",
            "$bucketAuto",
            "$collStats",
            "$count",
            "$currentOp",
            "$facet",
            "$geoNear",
            "$graphLookup",
            "$group",
            "$indexStats",
            "$limit",
            "$listLocalSessions",
            "$listSessions",
            "$lookup",
            "$match",
            "$out",
            "$project",
            "$redact",
            "$replaceRoot",
            "$sample" "$skip",
            "$sort",
            "$sortByCount",
            "$unwind",
        ]
        group_operators = [
            "$addToSet",
            "$first",
            "$last",
            "$max",
            "$min",
            "$avg",
            "$push",
            "$sum",
            "$stdDevPop",
            "$stdDevSamp",
        ]

        def _extend_collection(out_collection, field, expression):
            field_exists = False
            for doc in out_collection:
                if field in doc:
                    field_exists = True
                    break
            if not field_exists:
                for doc in out_collection:
                    if isinstance(expression, string_types) and expression.startswith(
                        "$"
                    ):
                        try:
                            doc[field] = get_value_by_dot(doc, expression.lstrip("$"))
                        except KeyError:
                            pass
                    else:
                        # verify expression has operator as first
                        doc[field] = aggregate.parse_expression(expression.copy(), doc)
            return out_collection

        def _accumulate_group(output_fields, group_list):
            doc_dict = {}
            for field, value in iteritems(output_fields):
                if field == "_id":
                    continue
                for operator, key in iteritems(value):
                    if operator in (
                        "$sum",
                        "$avg",
                        "$min",
                        "$max",
                        "$first",
                        "$last",
                        "$addToSet",
                        "$push",
                    ):
                        key_getter = functools.partial(aggregate.parse_expression, key)
                        values = [key_getter(doc) for doc in group_list]

                        if operator == "$sum":
                            val_it = (val or 0 for val in values)
                            doc_dict[field] = sum(val_it)
                        elif operator == "$avg":
                            values = [val or 0 for val in values]
                            doc_dict[field] = sum(values) / max(len(values), 1)
                        elif operator == "$min":
                            val_it = (val or MAXSIZE for val in values)
                            doc_dict[field] = min(val_it)
                        elif operator == "$max":
                            val_it = (val or -MAXSIZE for val in values)
                            doc_dict[field] = max(val_it)
                        elif operator == "$first":
                            doc_dict[field] = values[0]
                        elif operator == "$last":
                            doc_dict[field] = values[-1]
                        elif operator == "$addToSet":
                            value = []
                            val_it = (val or None for val in values)
                            # Don't use set in case elt in not hashable (like dicts).
                            for elt in val_it:
                                if elt not in value:
                                    value.append(elt)
                            doc_dict[field] = value
                        elif operator == "$push":
                            if field not in doc_dict:
                                doc_dict[field] = values
                            else:
                                doc_dict[field].extend(values)
                    elif operator in group_operators:
                        raise NotImplementedError(
                            "Although %s is a valid group operator for the "
                            "aggregation pipeline, it is currently not implemented "
                            "in Mongomock." % operator
                        )
                    else:
                        raise NotImplementedError(
                            "%s is not a valid group operator for the aggregation "
                            "pipeline. See http://docs.mongodb.org/manual/meta/"
                            "aggregation-quick-reference/ for a complete list of "
                            "valid operators." % operator
                        )
            return doc_dict

        out_collection = [doc for doc in self.find()]
        for stage in pipeline:
            for k, v in iteritems(stage):
                if k == "$match":
                    out_collection = [
                        doc for doc in out_collection if filter_applies(v, doc)
                    ]
                elif k == "$lookup":
                    for operator in ("let", "pipeline"):
                        if operator in stage["$lookup"]:
                            raise NotImplementedError(
                                "Although '%s' is a valid lookup operator for the "
                                "aggregation pipeline, it is currently not "
                                "implemented in Mongomock." % operator
                            )
                    for operator in ("from", "localField", "foreignField", "as"):
                        if operator not in stage["$lookup"]:
                            raise OperationFailure(
                                "Must specify '%s' field for a $lookup" % operator
                            )
                        if not isinstance(stage["$lookup"][operator], string_types):
                            raise OperationFailure(
                                "Arguments to $lookup must be strings"
                            )
                        if operator in ("as", "localField", "foreignField") and stage[
                            "$lookup"
                        ][operator].startswith("$"):
                            raise OperationFailure(
                                "FieldPath field names may not start with '$'"
                            )
                        if (
                            operator in ("localField", "as")
                            and "." in stage["$lookup"][operator]
                        ):
                            raise NotImplementedError(
                                "Although '.' is valid in the 'localField' and 'as' "
                                "parameters for the lookup stage of the aggregation "
                                "pipeline, it is currently not implemented in Mongomock."
                            )

                    foreign_name = stage["$lookup"]["from"]
                    local_field = stage["$lookup"]["localField"]
                    foreign_field = stage["$lookup"]["foreignField"]
                    local_name = stage["$lookup"]["as"]
                    foreign_collection = self.database.get_collection(foreign_name)
                    for doc in out_collection:
                        query = doc.get(local_field)
                        if isinstance(query, list):
                            query = {"$in": query}
                        matches = foreign_collection.find({foreign_field: query})
                        doc[local_name] = [foreign_doc for foreign_doc in matches]
                elif k == "$group":
                    grouped_collection = []
                    _id = stage["$group"]["_id"]
                    if _id:
                        key_getter = functools.partial(aggregate.parse_expression, _id)
                        sort_key_getter = _fix_sort_key(key_getter)
                        # Sort the collection only for the itertools.groupby.
                        # $group does not order its output document.
                        out_collection = sorted(out_collection, key=sort_key_getter)
                        grouped = itertools.groupby(out_collection, key_getter)
                    else:
                        grouped = [(None, out_collection)]

                    for doc_id, group in grouped:
                        group_list = [x for x in group]
                        doc_dict = _accumulate_group(v, group_list)
                        doc_dict["_id"] = doc_id
                        grouped_collection.append(doc_dict)

                    out_collection = grouped_collection

                elif k == "$bucket":
                    unknown_options = set(v) - {
                        "groupBy",
                        "boundaries",
                        "output",
                        "default",
                    }
                    if unknown_options:
                        raise OperationFailure(
                            "Unrecognized option to $bucket: %s."
                            % unknown_options.pop()
                        )
                    if "groupBy" not in v or "boundaries" not in v:
                        raise OperationFailure(
                            "$bucket requires 'groupBy' and 'boundaries' to be specified."
                        )
                    group_by = v["groupBy"]
                    boundaries = v["boundaries"]
                    if not isinstance(boundaries, list):
                        raise OperationFailure(
                            "The $bucket 'boundaries' field must be an array, but found type: %s"
                            % type(boundaries)
                        )
                    if len(boundaries) < 2:
                        raise OperationFailure(
                            "The $bucket 'boundaries' field must have at least 2 values, but "
                            "found %d value(s)." % len(boundaries)
                        )
                    if sorted(boundaries) != boundaries:
                        raise OperationFailure(
                            "The 'boundaries' option to $bucket must be sorted in ascending order"
                        )
                    output_fields = v.get("output", {"count": {"$sum": 1}})
                    default_value = v.get("default", None)
                    try:
                        is_default_last = default_value >= boundaries[-1]
                    except TypeError:
                        is_default_last = True

                    def _get_default_bucket():
                        try:
                            return v["default"]
                        except KeyError:
                            raise OperationFailure(
                                "$bucket could not find a matching branch for "
                                "an input, and no default was specified."
                            )

                    def _get_bucket_id(doc):
                        """Get the bucket ID for a document.

                        Note that it actually returns a tuple with the first
                        param being a sort key to sort the default bucket even
                        if it's not the same type as the boundaries.
                        """
                        try:
                            value = aggregate.parse_expression(group_by, doc)
                        except KeyError:
                            return (is_default_last, _get_default_bucket())
                        index = bisect.bisect_right(boundaries, value)
                        if index and index < len(boundaries):
                            return (False, boundaries[index - 1])
                        return (is_default_last, _get_default_bucket())

                    in_collection = (
                        (_get_bucket_id(doc), doc) for doc in out_collection
                    )
                    out_collection = sorted(in_collection, key=lambda kv: kv[0])
                    grouped = itertools.groupby(out_collection, lambda kv: kv[0])

                    out_collection = []
                    for (unused_key, doc_id), group in grouped:
                        group_list = [kv[1] for kv in group]
                        doc_dict = _accumulate_group(output_fields, group_list)
                        doc_dict["_id"] = doc_id
                        out_collection.append(doc_dict)

                elif k == "$sample":
                    if not isinstance(v, dict):
                        raise OperationFailure(
                            "the $sample stage specification must be an object"
                        )
                    size = v.pop("size", None)
                    if size is None:
                        raise OperationFailure("$sample stage must specify a size")
                    if v:
                        raise OperationFailure(
                            "unrecognized option to $sample: %s" % set(v).pop()
                        )
                    out_collection = [
                        _random.choice(out_collection) for i in range(size)
                    ]

                elif k == "$sort":
                    sort_array = []
                    for x, y in v.items():
                        sort_array.append({x: y})
                    for sort_pair in reversed(sort_array):
                        for sortKey, sortDirection in sort_pair.items():
                            out_collection = sorted(
                                out_collection,
                                key=lambda x: _resolve_sort_key(sortKey, x),
                                reverse=sortDirection < 0,
                            )
                elif k == "$skip":
                    out_collection = out_collection[v:]
                elif k == "$limit":
                    out_collection = out_collection[:v]
                elif k == "$unwind":
                    if not isinstance(v, dict):
                        v = {"path": v}
                    path = v["path"]
                    if not isinstance(path, string_types) or path[0] != "$":
                        raise ValueError(
                            "$unwind failed: exception: field path references must be prefixed "
                            "with a '$' '%s'" % path
                        )
                    path = path[1:]
                    should_preserve_null_and_empty = v.get("preserveNullAndEmptyArrays")
                    include_array_index = v.get("includeArrayIndex")
                    unwound_collection = []
                    for doc in out_collection:
                        try:
                            array_value = get_value_by_dot(doc, path)
                        except KeyError:
                            if should_preserve_null_and_empty:
                                unwound_collection.append(doc)
                            continue
                        if array_value is None:
                            if should_preserve_null_and_empty:
                                unwound_collection.append(doc)
                            continue
                        if array_value == []:
                            if should_preserve_null_and_empty:
                                new_doc = copy.deepcopy(doc)
                                delete_value_by_dot(new_doc, path)
                                unwound_collection.append(new_doc)
                            continue
                        if isinstance(array_value, list):
                            iter_array = enumerate(array_value)
                        else:
                            iter_array = [(None, array_value)]
                        for index, field_item in iter_array:
                            new_doc = copy.deepcopy(doc)
                            new_doc = set_value_by_dot(new_doc, path, field_item)
                            if include_array_index:
                                new_doc = set_value_by_dot(
                                    new_doc, include_array_index, index
                                )
                            unwound_collection.append(new_doc)

                    out_collection = unwound_collection
                elif k == "$project":
                    filter_list = []
                    method = None
                    include_id = v.get("_id")
                    for field, value in iteritems(v):
                        if "." in field:
                            raise NotImplementedError(
                                'Using subfield "%s" in $project is a valid MongoDB operation; '
                                "however Mongomock does not support it yet." % field
                            )
                        if method is None and (field != "_id" or value):
                            method = "include" if value else "exclude"
                        elif method == "include" and not value and field != "_id":
                            raise ValueError(
                                "Bad projection specification, cannot exclude fields "
                                "other than '_id' in an inclusion projection: %s" % v
                            )
                        elif method == "exclude" and value:
                            raise ValueError(
                                "Bad projection specification, cannot include fields "
                                "or add computed fields during an exclusion projection: %s"
                                % v
                            )
                        out_collection = _extend_collection(
                            out_collection, field, value
                        )
                        if field != "_id":
                            filter_list.append(field)
                    if (method == "include") == (include_id is not False):
                        filter_list.append("_id")
                    out_collection = [
                        {
                            k: v
                            for (k, v) in x.items()
                            if (method == "include") == (k in filter_list)
                        }
                        for x in out_collection
                    ]
                elif k == "$out":
                    # TODO(MetrodataTeam): should leave the origin collection unchanged
                    collection = self.database.get_collection(v)
                    if collection.count() > 0:
                        collection.drop()
                    collection.insert_many(out_collection)
                else:
                    if k in pipeline_operators:
                        raise NotImplementedError(
                            "Although '%s' is a valid operator for the aggregation pipeline, it is "
                            "currently not implemented in Mongomock." % k
                        )
                    else:
                        raise NotImplementedError(
                            "%s is not a valid operator for the aggregation pipeline. "
                            "See http://docs.mongodb.org/manual/meta/aggregation-quick-reference/ "
                            "for a complete list of valid operators." % k
                        )
        return CommandCursor(out_collection)

    def with_options(self, **kwargs):
        default_kwargs = {
            "codec_options": None,
            "read_preference": _READ_PREFERENCE_PRIMARY,
            "write_concern": None,
            "read_concern": None,
        }
        forbidden_kwargs = set(kwargs.keys()) - set(default_kwargs)
        if forbidden_kwargs:
            raise TypeError(
                "with_options() got an unexpected keyword argument '%s'"
                % forbidden_kwargs.pop()
            )
        for key, default_value in iteritems(default_kwargs):
            value = kwargs.get(key)
            if value is not None and value != default_value:
                raise NotImplementedError(
                    "%s is a valid parameter for with_options but it is currently not implemented "
                    "in Mongomock" % key
                )
        return self

    def rename(self, new_name, session=None, **kwargs):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        self.database.rename_collection(self.name, new_name, **kwargs)

    def bulk_write(
        self, requests, ordered=True, bypass_document_validation=False, session=None
    ):
        if not ordered:
            raise NotImplementedError(
                "Unordered mode is a valid MongoDB operation; however Mongomock"
                " does not support it yet."
            )
        if bypass_document_validation:
            raise NotImplementedError(
                "Skipping document validation is a valid MongoDB operation;"
                " however Mongomock does not support it yet."
            )
        if session:
            raise NotImplementedError(
                "Sessions are valid in MongoDB 3.6 and newer; however Mongomock"
                " does not support them yet."
            )
        bulk = BulkOperationBuilder(self)
        for operation in requests:
            operation._add_to_bulk(bulk)
        return BulkWriteResult(bulk.execute(), True)


def _resolve_key(key, doc):
    return next(iter(iter_key_candidates(key, doc)), NOTHING)


def _resolve_sort_key(key, doc):
    value = _resolve_key(key, doc)
    # see http://docs.mongodb.org/manual/reference/method/cursor.sort/#ascending-descending-sort
    if value is NOTHING:
        return 0, value

    return 1, value


def _fix_sort_key(key_getter):
    def fixed_getter(doc):
        key = key_getter(doc)
        # Convert dictionaries to make sorted() work in Python 3.
        if isinstance(key, dict):
            return [(k, v) for (k, v) in sorted(key.items())]
        return key

    return fixed_getter


class Cursor(object):
    def __init__(
        self,
        collection,
        spec=None,
        sort=None,
        projection=None,
        skip=0,
        limit=0,
        collation=None,
        no_cursor_timeout=False,
        batch_size=0,
        session=None,
    ):
        super(Cursor, self).__init__()
        self.collection = collection
        spec = helpers.patch_datetime_awareness_in_document(spec)
        self._spec = spec
        self._sort = sort
        self._projection = projection
        self._skip = skip
        self._factory_last_generated_results = None
        self._results = None
        self._factory = functools.partial(
            collection._get_dataset, spec, sort, projection, dict
        )
        # pymongo limit defaults to 0, returning everything
        self._limit = limit if limit != 0 else None
        self._collation = collation
        self.session = session
        self.rewind()

    def _compute_results(self, with_limit_and_skip=False):
        # Recompute the result only if the query has changed
        if not self._results or self._factory_last_generated_results != self._factory:
            if self.collection.database.client._tz_aware:
                results = [
                    helpers.make_datetime_timezone_aware_in_document(x)
                    for x in self._factory()
                ]
            else:
                results = list(self._factory())
            self._factory_last_generated_results = self._factory
            self._results = results
        if with_limit_and_skip:
            results = self._results[self._skip :]
            if self._limit:
                results = results[: self._limit]
        else:
            results = self._results
        return results

    def __iter__(self):
        return self

    def clone(self):
        cursor = Cursor(
            self.collection,
            self._spec,
            self._sort,
            self._projection,
            self._skip,
            self._limit,
        )
        cursor._factory = self._factory
        return cursor

    def __next__(self):
        try:
            doc = self._compute_results(with_limit_and_skip=True)[self._emitted]
            self._emitted += 1
            return doc
        except IndexError:
            raise StopIteration()

    next = __next__

    def rewind(self):
        self._emitted = 0

    def sort(self, key_or_list, direction=None):
        self._sort = helpers.create_index_list(key_or_list, direction)
        self._factory = functools.partial(
            self.collection._get_dataset, self._spec, self._sort, self._projection, dict)
        return self

    def count(self, with_limit_and_skip=False):
        results = self._compute_results(with_limit_and_skip)
        return len(results)

    def skip(self, count):
        self._skip = count
        return self

    def limit(self, count):
        self._limit = count if count != 0 else None
        return self

    def batch_size(self, count):
        return self

    def close(self):
        pass

    def distinct(self, key, session=None):
        if session:
            raise NotImplementedError("Mongomock does not handle sessions yet")
        if not isinstance(key, string_types):
            raise TypeError("cursor.distinct key must be a string")
        unique = set()
        unique_dict_vals = []
        for x in self._compute_results():
            value = _resolve_key(key, x)
            if value == NOTHING:
                continue
            if isinstance(value, dict):
                if any(dict_val == value for dict_val in unique_dict_vals):
                    continue
                unique_dict_vals.append(value)
            else:
                unique.update(value if isinstance(value, (tuple, list)) else [value])
        return list(unique) + unique_dict_vals

    async def to_list(self, length):
        result = [doc for doc in self]
        return result[:length]

    def __getitem__(self, index):
        if isinstance(index, slice):
            if index.step is not None:
                raise IndexError("Cursor instances do not support slice steps")

            skip = 0
            if index.start is not None:
                if index.start < 0:
                    raise IndexError(
                        "Cursor instances do not support" "negative indices"
                    )
                skip = index.start

            if index.stop is not None:
                limit = index.stop - skip
                if limit < 0:
                    raise IndexError(
                        "stop index must be greater than start"
                        "index for slice %r" % index
                    )
                if limit == 0:
                    self.__empty = True
            else:
                limit = 0

            self._skip = skip
            self._limit = limit
            return self
        elif not isinstance(index, int):
            raise TypeError("index '%s' cannot be applied to Cursor instances" % index)
        elif index < 0:
            raise IndexError("Cursor instances do not support negativeindices")
        else:
            return self._compute_results(with_limit_and_skip=True)[index]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @property
    def alive(self):
        return self._emitted != self.count()


def _set_updater(doc, field_name, value):
    if isinstance(value, (tuple, list)):
        value = copy.deepcopy(value)
    if isinstance(doc, dict):
        if BSON:
            # bson validation
            BSON.encode({field_name: value}, check_keys=True)
        doc[field_name] = value


def _unset_updater(doc, field_name, value):
    if isinstance(doc, dict):
        doc.pop(field_name, None)


def _inc_updater(doc, field_name, value):
    if isinstance(doc, dict):
        doc[field_name] = doc.get(field_name, 0) + value


def _max_updater(doc, field_name, value):
    if isinstance(doc, dict):
        doc[field_name] = max(doc.get(field_name, value), value)


def _min_updater(doc, field_name, value):
    if isinstance(doc, dict):
        doc[field_name] = min(doc.get(field_name, value), value)


def _sum_updater(doc, field_name, current, result):
    if isinstance(doc, dict):
        result = current + doc.get[field_name, 0]
        return result


def _current_date_updater(doc, field_name, value):
    if isinstance(doc, dict):
        doc[field_name] = datetime.utcnow()


_updaters = {
    "$set": _set_updater,
    "$unset": _unset_updater,
    "$inc": _inc_updater,
    "$max": _max_updater,
    "$min": _min_updater,
}
