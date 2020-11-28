from __future__ import annotations

import datetime
import importlib
import json
from enum import Enum
from typing import Any, Dict

import immutables

from eventz.protocols import MarshallCodecProtocol, MarshallProtocol


class Marshall(MarshallProtocol):
    def __init__(
        self,
        fqn_resolver: FqnResolverProtocol,
        codecs: Dict[str, MarshallCodecProtocol] = None,
    ):
        self._fqn_resolver: FqnResolverProtocol = fqn_resolver
        self._codecs = {} if codecs is None else codecs

    def register_codec(self, fcn: str, codec: MarshallCodecProtocol):
        self._codecs[fcn] = codec

    def deregister_codec(self, fcn: str):
        del self._codecs[fcn]

    def has_codec(self, fcn: str):
        return fcn in self._codecs

    def to_json(self, data: Any) -> str:
        data = self.serialise_data(data)
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    def from_json(self, json_string: str) -> Any:
        data = json.loads(json_string)
        return self.deserialise_data(data)

    def serialise_data(self, data: Any) -> Any:
        if self._is_handled_by_codec(data):
            return self._object_to_codec_dict(data)
        elif self._is_sequence(data):
            new_sequence = []
            for item in data:
                new_sequence.append(self.serialise_data(item))
            return new_sequence
        elif self._is_mapping(data):
            new_mapping = {}
            for key, value in data.items():
                new_mapping[key] = self.serialise_data(value)
            return new_mapping
        elif self._is_simple_type(data):
            return data
        else:
            return self._object_to_dict(data)

    def deserialise_data(self, data: Any) -> Any:
        if self._is_enum_dict(data):
            return self._dict_to_enum(data)
        if self._is_serialised_class(data):
            return self._dict_to_object(data)
        elif self._requires_codec(data):
            return self._codec_dict_to_object(data)
        elif self._is_sequence(data):
            new_sequence = []
            for item in data:
                new_sequence.append(self.deserialise_data(item))
            return new_sequence
        elif self._is_mapping(data):
            new_mapping = {}
            for key, value in data.items():
                new_mapping[key] = self.deserialise_data(value)
            return immutables.Map(new_mapping)
        else:  # all other simple types now
            return data

    def _object_to_dict(self, obj: Any) -> Dict:
        data = {
            "__fqn__": self._fqn_resolver.instance_to_fqn(obj)
        }
        if hasattr(obj, "__version__"):
            data["__version__"] = obj.__version__
        if hasattr(obj, "__msgid__"):
            data["__msgid__"] = obj.__msgid__
        if hasattr(obj, "__timestamp__"):
            data["__timestamp__"] = self.serialise_data(obj.__timestamp__)
        if hasattr(obj, "get_json_data") and callable(obj.get_json_data):
            json_data = obj.get_json_data()
        else:
            json_data = vars(obj)
        for attr, value in json_data.items():
            if not attr.startswith("__"):
                data[attr] = self.serialise_data(value)
        return data

    def _dict_to_object(self, data: Dict) -> Any:
        kwargs = {}
        if data.get("__msgid__"):
            kwargs["__msgid__"] = data.get("__msgid__")
        if data.get("__timestamp__"):
            kwargs["__timestamp__"] = self.deserialise_data(data.get("__timestamp__"))
        for key, value in data.items():
            if not key.startswith("__"):
                kwargs[key] = self.deserialise_data(value)
        # @TODO add "allowed_namespaces" list to class and do a check here to protect against code injection
        _class = self._fqn_resolver.fqn_to_type(data["__fqn__"])
        return _class(**kwargs)

    def _codec_dict_to_object(self, data: Dict) -> Any:
        fcn = data["__codec__"]
        return self._codecs[fcn].deserialise(data["params"])

    def _object_to_codec_dict(self, obj: Any) -> Dict:
        for codec in self._codecs.values():
            if codec.handles(obj):
                return codec.serialise(obj)

    def _dict_to_enum(self, data: Dict) -> Enum:
        # @TODO add "allowed_namespaces" list to class and do a check here to protect against code injection
        _class = self._fqn_resolver.fqn_to_type(data["__fqn__"])
        return getattr(_class, data["_name_"])

    def _is_handled_by_codec(self, data: Any) -> bool:
        return any([codec.handles(data) for codec in self._codecs.values()])

    def _is_sequence(self, data: Any) -> bool:
        return isinstance(data, (list, tuple))

    def _is_mapping(self, data: Any) -> bool:
        return isinstance(data, (dict, set, immutables.Map))

    def _is_enum_dict(self, data: Dict) -> bool:
        return isinstance(data, Dict) and "_value_" in data and "_name_" in data

    def _is_simple_type(self, data: Any) -> bool:
        if type(data).__module__ == "builtins":
            return True
        # now check for any other types we want to treat as simple
        return isinstance(data, (datetime.datetime,))

    def _is_serialised_class(self, data: Any) -> bool:
        return isinstance(data, dict) and "__fqn__" in data

    def _requires_codec(self, data: Any) -> bool:
        return isinstance(data, dict) and "__codec__" in data


class NoCodecError(Exception):
    pass


class FqnResolverProtocol:
    def fqn_to_type(self, fqn: str) -> type:
        ...

    def instance_to_fqn(self, instance: Any) -> str:
        ...


class FqnResolver(FqnResolverProtocol):
    def __init__(self, fqn_map: Dict):
        """
        The "public" side of the map is the fqn written into the JSON payloads.
        The "private" side of the map is whatever path is needed to help the
        client code transform the fqn into an instance.
        """
        self._public_to_private: Dict = fqn_map
        self._private_to_public: Dict = {b: a for a, b in fqn_map.items()}

    def fqn_to_type(self, fqn: str) -> type:
        module_path = self._get_fqn(fqn, public=True)
        module_name, class_name = module_path.rsplit(".", 1)
        return getattr(importlib.import_module(module_name), class_name)

    def instance_to_fqn(self, instance: Any) -> str:
        path = instance.__class__.__module__ + "." + instance.__class__.__name__
        return self._get_fqn(path, public=False)

    def _get_fqn(self, key: str, public: bool) -> str:
        try:
            return self._lookup_fqn(key, public)
        except KeyError as e:
            # can we resolve with * path?
            if key[-1] != "*" and "." in key:
                parts = key.split(".")
                entity = parts.pop()
                star_key = ".".join(parts + ["*"])
                path = self._lookup_fqn(star_key, public)
                path_without_star = path[:-1]
                return path_without_star + entity
            raise e

    def _lookup_fqn(self, key: str, public: bool) -> str:
        if public:
            return self._public_to_private[key]
        else:
            return self._private_to_public[key]
