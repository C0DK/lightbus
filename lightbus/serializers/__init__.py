import inspect
import json
from typing import Union, Any, TypeVar, Type

import lightbus
from lightbus.exceptions import InvalidMessage, InvalidSerializerConfiguration


def decode_bytes(b: Union[str, bytes]):
    return b if isinstance(b, str) else b.decode('utf8')


def sanity_check_metadata(message_class, metadata):
    """Takes unserialized metadata and checks it looks sane

    This relies upon the required_metadata of each Message class
    to provide a list of metadata fields that are required.
    """
    for required_key in message_class.required_metadata:
        if required_key not in metadata:
            raise InvalidMessage(
                "Required key '{key}' missing in {cls} metadata. "
                "Found keys: {keys}".format(
                    key=required_key,
                    keys=', '.join(metadata.keys()),
                    cls=message_class.__name__
                )
            )
        elif not metadata.get(required_key):
            raise InvalidMessage(
                "Required key '{key}' present in {cls} metadata but value was empty"
                "".format(
                    key=required_key,
                    cls=message_class.__name__
                )
            )


SerialisedData = TypeVar('SerialisedData')


class MessageSerializer(object):

    def __init__(self, encoder=json.dumps):
        self.encoder = encoder

    def __call__(self, message: 'lightbus.Message') -> SerialisedData:
        raise NotImplementedError()


class MessageDeserializer(object):

    def __init__(self, message_class: Type['lightbus.Message'], decoder=json.loads):
        if not inspect.isclass(message_class):
            raise InvalidSerializerConfiguration(
                "The message_class value provided to JsonMessageDeserializer was not a class, "
                "it was actually: {}".format(message_class)
            )

        self.message_class = message_class
        self.decoder = decoder

    def __call__(self, serialized: SerialisedData) -> 'lightbus.Message':
        raise NotImplementedError()
