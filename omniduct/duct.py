import atexit
import decorator
import functools
import getpass
import inspect
import os
import pwd
import re
import textwrap
from abc import ABCMeta, abstractmethod
from builtins import input
from enum import Enum

import six
from future.utils import raise_with_traceback, with_metaclass

from omniduct.errors import DuctServerUnreachable, DuctProtocolUnknown
from omniduct.utils.debug import logger, logging_scope
from omniduct.utils.dependencies import check_dependencies
from omniduct.utils.docs import quirk_docs
from omniduct.utils.ports import naive_load_balancer, is_port_bound


class ProtocolRegisteringABCMeta(ABCMeta):
    """
    This metaclass provides automatic registration of Duct subclasses so that
    they can be looked up by the protocols they support. Note that protocol
    mappings must be unique.
    """

    def __init__(cls, name, bases, dct):
        ABCMeta.__init__(cls, name, bases, dct)

        if not hasattr(cls, '_protocols'):
            cls._protocols = {}

        registry_keys = getattr(cls, 'PROTOCOLS', []) or []
        if registry_keys:
            for key in registry_keys:
                if key in cls._protocols and cls.__name__ != cls._protocols[key].__name__:
                    logger.info("Ignoring attempt by class `{}` to register key '{}', which is already registered for class `{}`.".format(cls.__name__, key, cls._protocols[key].__name__))
                else:
                    cls._protocols[key] = cls

    def _for_protocol(cls, key):
        if key not in cls._protocols:
            raise DuctProtocolUnknown("Missing `Duct` implementation for protocol: '{}'.".format(key))
        return cls._protocols[key]


class ProtocolRegisteringQuirkDocumentedABCMeta(ProtocolRegisteringABCMeta):
    """
    This metaclass adds the ability to automatically append quirk documentation
    to methods from a nominated method. For example, if the protocol specific
    implementation of `.connect()` is implemented in `._connect`, you can
    decorate the connect method with this decorator using
    `@quirk_docs('_connect')`, the the documentation from the `_connect`
    method will be appended to the `connect` docs under a heading "<cls> Quirks:".
    """

    def __init__(cls, name, bases, dct):
        super(ProtocolRegisteringQuirkDocumentedABCMeta, cls).__init__(name, bases, dct)

        # Allow method of avoiding appending of quirk docs in some environments (such as documentation)
        if os.environ.get('OMNIDUCT_DISABLE_QUIRKDOCS', None) is not None:
            return

        @decorator.decorator
        def wrapped(f, *args, **kw):
            return f(*args, **kw)

        mro = inspect.getmro(cls)
        mro = mro[:[klass.__name__ for klass in mro].index('Duct') + 1]

        # Handle module-level documentation
        module_docs = [cls.__doc__]
        for klass in mro:
            if klass != cls and hasattr(klass, '_{}__doc_attrs'.format(klass.__name__)):
                module_docs.append([
                    'Attributes inherited from {}:'.format(klass.__name__),
                    inspect.cleandoc(getattr(klass, '_{}__doc_attrs'.format(klass.__name__)))
                ])

        cls.__doc__ = cls.__doc_join(*module_docs)

        # Handle function/method-level documentation
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction):

            # Check if there is anything to do
            if (
                inspect.isabstract(member) or
                not (
                    getattr(member, '_quirks_method', None) or
                    getattr(member, '_quirks_mro', False)
                )
            ):
                continue

            local_member = name in cls.__dict__

            # Extract documentation from this member and the quirks member
            member_docs = getattr(member, '__doc_orig__', None) or getattr(member, '__doc__')
            mro_docs = quirk_docs = None
            mro_order = reversed(mro) if member._quirks_mro_reverse else mro
            if member._quirks_mro:
                mro_docs = cls.__doc_join(
                    *[
                        [
                            'Inherited via {}:'.format(klass.__name__),
                            getattr(getattr(klass, member.__name__), '__doc_orig__', None) or getattr(klass, member.__name__).__doc__
                        ]
                        for klass in mro_order if member.__name__ in klass.__dict__
                    ]
                )
            if member._quirks_method and member._quirks_method in cls.__dict__:
                quirk_member = getattr(cls, member._quirks_method, None)
                if quirk_member:
                    quirk_docs = getattr(quirk_member, '__doc_orig__', None) or getattr(quirk_member, '__doc__')

            if quirk_docs or mro_docs:
                # Overide method object with new object so we don't modify
                # underlying method that may be shared by multiple classes.
                setattr(cls, name, wrapped(member))
                member = getattr(cls, name)
                member.__doc__ = cls.__doc_join(
                    member_docs if (local_member or not mro_docs) else None,
                    mro_docs,
                    [
                        "{} Quirks:".format(cls.__name__),
                        quirk_docs
                    ]
                )

    @classmethod
    def __doc_join(cls, *docs, **kwargs):
        out = []
        for doc in docs:
            if doc in (None, ''):
                continue
            elif isinstance(doc, six.string_types):
                out.append(textwrap.dedent(doc).strip('\n'))
            elif isinstance(doc, (list, tuple)):
                if len(doc) < 2:
                    continue
                d = cls.__doc_join(*doc[1:])
                if d:
                    out.append(
                        '{header}\n{body}'.format(
                            header=doc[0].strip(),
                            body='    ' + d.replace('\n', '\n    ')  # textwrap.indent not available in python2
                        )
                    )
            else:
                raise ValueError("Unrecognised doc format: {}".format(type(doc)))
        return '\n\n'.join(out)


class Duct(with_metaclass(ProtocolRegisteringQuirkDocumentedABCMeta, object)):
    """
    `Duct` is the abstract base class of all protocol implementations, and
    defines the basic lifecycle of all connections, along with some magic
    that provides automatic registration of Duct protocol implementations.
    All connections made by `Duct` instances are lazy, meaning that instantiation
    is "free", and no protocol connections are made until required by subsequent
    interactions (i.e. when the value of any attribute in the list of
    `connection_fields` is accessed). All `Ducts` will automatically connnect and
    disconnect as required, and so manual intervention is not typically required
    to maintain connections.

    Attributes:
        protocol (str): The name of the protocol for which this instance was
            created (especially useful if a `Duct` subclass supports multiple
            protocols).
        name (str): The name given to this `Duct` instance (defaults to class
            name).
        registry (None, omniduct.registry.DuctRegistry): A reference to a
            `DuctRegistry` instance for runtime lookup of other services.
        remote (None, omniduct.remotes.base.RemoteClient): A reference to a
            `RemoteClient` instance to manage connections to remote services.
        cache (None, omniduct.caches.base.Cache): A reference to a `Cache`
            instance to add support for caching, if applicable.
        connection_fields (tuple<str>, list<str>): A list of instance attributes
            to monitor for changes, whereupon the `Duct` instance should automatically
            disconnect. By default, the following attributes are monitored:
            'host', 'port', 'remote', 'username', and 'password'.
        prepared_fields (tuple<str>, list<str>): A list of instance attributes to
            be populated (if their values are callable) when the instance first
            connects to a service. Refer to `Duct.prepare` and `Duct._prepare` for
            more details. By default, the following attributes are prepared:
            '_host', '_port', '_username', and '_password'.

    Additional attributes including `host`, `port`, `username` and `password` are
    documented inline below.

    Class Attributes:
        AUTO_LOGGING_SCOPE (bool): Whether this class should be used by omniduct
            logging code as a "scope". Should be overridden by subclasses as
            appropriate.
        DUCT_TYPE (Duct.Type): The type of `Duct` service that is provided by
            this Duct instance. Should be overridden by subclasses as
            appropriate.
        PROTOCOLS (list<str>): The name(s) of any protocols that should be
            associated with this class. Should be overridden by subclasses as
            appropriate.
    """
    __doc_attrs = """
        protocol (str): The name of the protocol for which this instance was
            created (especially useful if a `Duct` subclass supports multiple
            protocols).
        name (str): The name given to this `Duct` instance (defaults to class
            name).
        host (str): The host name providing the service (will be '127.0.0.1', if
            service is port forwarded from remote; use `._host` to see remote
            host).
        port (int): The port number of the service (will be the port-forwarded
            local port, if relevant; for remote port use `._port`).
        username (str, bool): The username to use for the service.
        password (str, bool): The password to use for the service.
        registry (None, omniduct.registry.DuctRegistry): A reference to a
            `DuctRegistry` instance for runtime lookup of other services.
        remote (None, omniduct.remotes.base.RemoteClient): A reference to a
            `RemoteClient` instance to manage connections to remote services.
        cache (None, omniduct.caches.base.Cache): A reference to a `Cache`
            instance to add support for caching, if applicable.
        connection_fields (tuple<str>, list<str>): A list of instance attributes
            to monitor for changes, whereupon the `Duct` instance should automatically
            disconnect. By default, the following attributes are monitored:
            'host', 'port', 'remote', 'username', and 'password'.
        prepared_fields (tuple<str>, list<str>): A list of instance attributes to
            be populated (if their values are callable) when the instance first
            connects to a service. Refer to `Duct.prepare` and `Duct._prepare` for
            more details. By default, the following attributes are prepared:
            '_host', '_port', '_username', and '_password'.
    """
    __doc_cls_attrs__ = None

    class Type(Enum):
        """
        The `Duct.Type` enum specifies all of the permissible values of
        `Duct.DUCT_TYPE`.
        """
        REMOTE = 'remotes'
        FILESYSTEM = 'filesystems'
        DATABASE = 'databases'
        CACHE = 'caches'
        RESTFUL = 'rest_clients'
        OTHER = 'other'

    AUTO_LOGGING_SCOPE = True
    DUCT_TYPE = None
    PROTOCOLS = None

    def __init__(self, protocol=None, name=None, registry=None, remote=None,
                 host=None, port=None, username=None, password=None, cache=None):
        """
        protocol (str, None): Name of protocol (used by Duct registries to inform
            Duct instances of how they were instantiated).
        name (str, None): The name to used by the `Duct` instance (defaults to
            class name if not specified).
        registry (DuctRegistry, None): The registry to use to lookup remote
            and/or cache instance specified by name.
        remote (str, RemoteClient): The remote by which the ducted service
            should be contacted.
        host (str): The hostname of the service to be used by this client.
        port (int): The port of the service to be used by this client.
        username (str, bool, None): The username to authenticate with if necessary.
            If True, then users will be prompted at runtime for credentials.
        password (str, bool, None): The password to authenticate with if necessary.
            If True, then users will be prompted at runtime for credentials.
        cache(Cache, None): The cache client to be attached to this instance.
            Cache will only used by specific methods as configured by the client.
        """

        check_dependencies(self.PROTOCOLS)

        self.protocol = protocol
        self.name = name or self.__class__.__name__
        self.registry = registry
        self.remote = remote
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.cache = cache

        self.connection_fields = ('host', 'port', 'remote', 'username', 'password')
        self.prepared_fields = ('_host', '_port', '_username', '_password')

        atexit.register(self.disconnect)
        self.__prepared = False
        self.__getting = False
        self.__disconnecting = False
        self.__cached_auth = {}
        self.__prepreparation_values = {}

    @property
    def __prepare_triggers(self):
        return (
            ('cache',)
            + object.__getattribute__(self, 'connection_fields')
        )

    @classmethod
    def __init_with_kwargs__(cls, self, kwargs, **fallbacks):
        if not hasattr(self, '_Duct__inited_using_kwargs'):
            self._Duct__inited_using_kwargs = {}
        for cls_parent in reversed([parent for parent in inspect.getmro(cls) if issubclass(parent, Duct) and parent not in self._Duct__inited_using_kwargs and '__init__' in parent.__dict__]):
            self._Duct__inited_using_kwargs[cls_parent] = True
            if six.PY3:
                argspec = inspect.getfullargspec(cls_parent.__init__)
                keys = argspec.args[1:] + argspec.kwonlyargs
            else:
                keys = inspect.getargspec(cls_parent.__init__).args[1:]
            params = {}
            for key in keys:
                if key in kwargs:
                    params[key] = kwargs.pop(key)
                elif key in fallbacks:
                    params[key] = fallbacks[key]
            cls_parent.__init__(self, **params)

    @classmethod
    def for_protocol(cls, key):
        """
        This classmethod retrieves the appropriate `Duct` subclass to connect to
        the provided protocol. If no subclass of `Duct` has been created
        in the current Python session, a `DuctProtocolUnknown` error is
        thrown.

        Parameters:
            key (str): The protocol of interest.

        Returns:
            `Duct` subclass: The appropriate class for the provided protocol.

        Raises:
            DuctProtocolUnknown: If no class has been defined that offers the
                named protocol.
        """
        return functools.partial(cls._for_protocol(key), protocol=key)

    def __getattribute__(self, key):
        try:
            if (not object.__getattribute__(self, '_Duct__prepared')
                    and not object.__getattribute__(self, '_Duct__getting')
                    and not object.__getattribute__(self, '_Duct__disconnecting')
                    and key in object.__getattribute__(self, '_Duct__prepare_triggers')):
                object.__setattr__(self, '_Duct__getting', True)
                object.__getattribute__(self, 'prepare')()
                object.__setattr__(self, '_Duct__getting', False)
        except AttributeError:
            pass
        except Exception as e:
            object.__setattr__(self, '_Duct__getting', False)
            raise_with_traceback(e)
        return object.__getattribute__(self, key)

    def __setattr__(self, key, value):
        try:
            if (getattr(self, '_Duct__prepared', False)
                    and getattr(self, 'connection_fields', None)
                    and key in self.connection_fields
                    and self.is_connected()):
                logger.warn('Disconnecting prior to changing field that connection is based on: {}.'.format(key))
                self.disconnect()
                self.__prepared = False
        except AttributeError:
            pass
        object.__setattr__(self, key, value)

    @quirk_docs('_prepare')
    def prepare(self):
        """
        This method is called before the value of any of the fields referenced
        in `self.connection_fields` are retrieved, and does not return anything.
        The fields include, by default: 'host', 'port', 'remote', 'cache',
        'username', and 'password'. Subclasses may add or subtract from these
        special fields.

        When called, it first checks whether the instance has already been prepared,
        and if not calls `_prepare` and then records that the instance has been
        successfully prepared.
        """
        if not self.__prepared:
            self._prepare()
            self.__prepared = True

    def _prepare(self):
        """
        This method may be overridden by subclasses, but provides the following
        default behaviour:

         - Ensures `self.registry`, `self.remote` and `self.cache` values are
           instances of the right types.
         - It replaces string values of `self.remote` and `self.cache` with
           remotes and caches looked up using `self.registry.lookup`.
         - It looks through each of the fields nominated in `self.prepared_fields`
           and, if the corresponding value is callable, sets the value of that
           field to result of calling that value with a reference to `self`. By
           default, `prepared_fields` contains '_host', '_port', '_username',
           and '_password'.
         - Ensures value of self.port is an integer (or None).
        """
        # Import necessary classes lazily (to prevent dependency cycles)
        from omniduct.registry import DuctRegistry
        from omniduct.caches.base import Cache
        from omniduct.remotes.base import RemoteClient

        # Check registry is of an appropriate type (if present)
        assert (self.registry is None) or isinstance(self.registry, DuctRegistry), "Provided registry is not an instance of `omniduct.registry.DuctRegistry`."

        # If registry is present, lookup remotes and caches if necessary
        if self.registry is not None:
            if self.remote and isinstance(self.remote, six.string_types):
                self.__prepreparation_values['remote'] = self.remote
                self.remote = self.registry.lookup(self.remote, kind=Duct.Type.REMOTE)
            if self.cache and isinstance(self.cache, six.string_types):
                self.__prepreparation_values['cache'] = self.cache
                self.cache = self.registry.lookup(self.cache, kind=Duct.Type.CACHE)

        # Check if remote and cache objects are of correct type (if present)
        assert (self.remote is None) or isinstance(self.remote, RemoteClient), "Provided remote is not an instance of `omniduct.remotes.base.RemoteClient`."
        assert (self.cache is None) or isinstance(self.cache, Cache), "Provided cache is not an instance of `omniduct.caches.base.Cache`."

        # Replace prepared fields with the result of calling existing values
        # with a reference to `self`.
        for field in self.prepared_fields:
            value = getattr(self, field)
            if hasattr(value, '__call__'):
                self.__prepreparation_values[field] = value
                setattr(self, field, value(self))

        if isinstance(self._host, (list, tuple)):
            if '_host' not in self.__prepreparation_values:
                self.__prepreparation_values['_host'] = self._host
            self._host = naive_load_balancer(self._host, port=self._port)

        # If host has a port included in it, override the value of self._port
        if self._host is not None and re.match(r'[^\:]+:[0-9]{1,5}', self._host):
            self._host, self._port = self._host.split(':')

        # Ensure port is an integer value
        self.port = int(self._port) if self._port else None

    @property
    def host(self):
        """
        str: The host name providing the service, or '127.0.0.1' if `self.remote` is
        not `None`, whereupon the service will be port-forwarded locally. You can
        view the remote hostname using `duct._host`, and change the remote host
        at runtime using: `duct.host = '<host>'`.
        """
        if self.remote:
            return '127.0.0.1'  # TODO: Make this configurable.
        return self._host

    @host.setter
    def host(self, host):
        self._host = host

    @property
    def port(self):
        """
        int: The local port for the service. If `self.remote` is not `None`, the
        port will be port-forwarded from the remote host. To see the port used on
        the remote host refer to `duct._port`. You can change the remote port
        at runtime using: `duct.port = <port>`.
        """
        if self.remote:
            return self.remote.port_forward('{}:{}'.format(self._host, self._port))
        return self._port

    @port.setter
    def port(self, port):
        self._port = port

    @property
    def username(self):
        """
        str: Some services require authentication in order to connect to the
        service, in which case the appropriate username can be specified. If not
        specified at instantiation, your local login name will be used. If `True`
        was provided, you will be prompted to type your username at runtime as
        necessary. If `False` was provided, then `None` will be returned. You can
        specify a different username at runtime using: `duct.username = '<username>'`.
        """
        if self._username is True:
            if 'username' not in self.__cached_auth:
                self.__cached_auth['username'] = input("Enter username for '{}':".format(self.name))
            return self.__cached_auth['username']
        elif self._username is False:
            return None
        elif not self._username:
            try:
                username = os.getlogin()
            except OSError:
                username = pwd.getpwuid(os.geteuid()).pw_name
            return username
        return self._username

    @username.setter
    def username(self, username):
        self._username = username

    @property
    def password(self):
        """
        str: Some services require authentication in order to connect to the
        service, in which case the appropriate password can be specified. If
        `True` was provided at instantiation, you will be prompted to type your
        password at runtime when necessary. If `False` was provided, then
        `None` will be returned. You can specify a different password at runtime
        using: `duct.password = '<password>'`.
        """
        if self._password is True:
            if 'password' not in self.__cached_auth:
                self.__cached_auth['password'] = getpass.getpass("Enter password for '{}':".format(self.name))
            return self.__cached_auth['password']
        elif self._password is False:
            return None
        return self._password

    @password.setter
    def password(self, password):
        self._password = password

    def __assert_server_reachable(self):
        if self.host is not None or self.port is not None:
            if self.host is None:
                raise ValueError("Port specified but no host provided.")
            if self.port is None:
                raise ValueError("Host specified but no port specified.")
        else:
            return

        if not is_port_bound(self.host, self.port):
            if self.remote and not self.remote.is_port_bound(self._host, self._port):
                self.disconnect()
                raise DuctServerUnreachable(
                    "Remote '{}' cannot connect to '{}:{}'. Please check your settings before trying again.".format(
                        self.remote.name, self._host, self._port))
            elif not self.remote:
                self.disconnect()
                raise DuctServerUnreachable(
                    "Cannot connect to '{}:{}' on your current connection. Please check your connection before trying again.".format(
                        self.host, self.port))

    # Connection
    @logging_scope("Connecting")
    @quirk_docs('_connect')
    def connect(self):
        """
        This method causes the `Duct` instance to connect to the service, if it
        is not already connected. It is not normally necessary for a user to
        manually call this function, since when a connection is required, it is
        automatically created.

        Subclasses should implement `Duct._connect` to do whatever is necessary
        to bring a connection into being.

        Returns:
            `Duct` instance: A reference to the current object.
        """
        if self.host:
            logger.info(
                "Connecting to {host}:{port}{remote}.".format(
                    host=self._host,
                    port=self._port,
                    remote="on {}".format(self.remote.host) if self.remote else ""
                )
            )
        self.__assert_server_reachable()
        if not self.is_connected():
            try:
                self._connect()
            except Exception as e:
                self.reset()
                raise_with_traceback(e)
        if self.host:
            logger.info(
                "Connected to {host}:{port}{remote}.".format(
                    host=self._host,
                    port=self._port,
                    remote="on {}".format(self.remote.host) if self.remote else ""
                )
            )
        return self

    @abstractmethod
    def _connect(self):
        """
        This method should be overridden by subclasses, and when called, should
        create a connection to the appropriate service. It is not necessary for
        this method to return anything.
        """
        raise NotImplementedError

    @quirk_docs('_is_connected')
    def is_connected(self):
        """
        This method checks to see whether a `Duct` instance is currently
        connected. This is performed by verifying that the remote host and port
        are still accessible, and then by calling `Duct._is_connected`, which
        should be implemented by subclasses.

        Returns:
            bool: Whether this `Duct` instance is currently connected.
        """
        if not self.__prepared:
            return False

        if self.remote:
            if not self.remote.has_port_forward(self._host, self._port):
                return False
            elif not is_port_bound(self.host, self.port):
                self.disconnect()
                return False

        return self._is_connected()

    @abstractmethod
    def _is_connected(self):
        """
        This method should be implemented by subclasses and return `True` when
        this `Duct` instance is connected, and `False` otherwise.
        """
        raise NotImplementedError

    @quirk_docs('_disconnect')
    def disconnect(self):
        """
        This method disconnects this `Duct` instance from the service, and is
        automatically called during reconnections and/or at Python interpreter
        shutdown. It first calls `Duct._disconnect` (which should be implemented
        by subclasses) and then notifies the `RemoteClient` subclass, if present,
        to stop port-forwarding the remote service.

        Returns:
            `Duct` instance: A reference to this object.
        """
        if not self.__prepared:
            return

        self.__disconnecting = True

        try:
            self._disconnect()

            if self.remote and self.remote.has_port_forward(self._host, self._port):
                logger.info('Freeing up local port {0}...'.format(self.port))
                self.remote.port_forward_stop(local_port=self.port)
        finally:
            self.__disconnecting = False

        return self

    @abstractmethod
    def _disconnect(self):
        """
        Subclasses should implement this method to disconnect from remote
        services. The return value of this method is not used.
        """
        raise NotImplementedError

    def reconnect(self):
        """
        Disconnects, and then reconnects, this client. This is entirely equivalent
        to `duct.disconnect().connect()`.

        Returns:
            `Duct` instance: A reference to this object.
        """
        return self.disconnect().connect()

    def reset(self):
        """
        This method resets the `Duct` instance to it's pre-preparation state. In
        particular it disconnects from the service, resets any temporary
        authentication and restores the values of the attributes listed in
        `prepared_fields` to their values as of when `Duct.prepare` was called.

        Returns:
            `Duct` instance: A reference to this object.
        """
        self.disconnect()
        self.__cached_auth = {}

        for key, value in self.__prepreparation_values.items():
            setattr(self, key, value)
        self.__prepreparation_values = {}
        self.__prepared = False

        return self
