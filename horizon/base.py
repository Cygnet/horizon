# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 Nebula, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Contains the core classes and functionality that makes Horizon what it is.
This module is considered internal, and should not be relied on directly.

Public APIs are made available through the :mod:`horizon` module and
the classes contained therein.
"""

import collections
import copy
import inspect
import logging

from django.conf import settings
from django.conf.urls.defaults import patterns, url, include
from django.core.exceptions import ImproperlyConfigured
from django.core.urlresolvers import reverse
from django.utils.datastructures import SortedDict
from django.utils.functional import SimpleLazyObject
from django.utils.importlib import import_module
from django.utils.module_loading import module_has_submodule
from django.utils.translation import ugettext as _

from horizon.decorators import (require_auth, require_roles,
                                require_services, _current_component)


LOG = logging.getLogger(__name__)


# Default configuration dictionary. Do not mutate directly. Use copy.copy().
HORIZON_CONFIG = {
    # Allow for ordering dashboards; list or tuple if provided.
    'dashboards': None,
    # Name of a default dashboard; defaults to first alphabetically if None
    'default_dashboard': None,
    'user_home': None,
    'exceptions': {'unauthorized': [],
                   'not_found': [],
                   'recoverable': []}
}


def _decorate_urlconf(urlpatterns, decorator, *args, **kwargs):
    for pattern in urlpatterns:
        if getattr(pattern, 'callback', None):
            pattern._callback = decorator(pattern.callback, *args, **kwargs)
        if getattr(pattern, 'url_patterns', []):
            _decorate_urlconf(pattern.url_patterns, decorator, *args, **kwargs)


class NotRegistered(Exception):
    pass


class HorizonComponent(object):
    def __init__(self):
        super(HorizonComponent, self).__init__()
        if not self.slug:
            raise ImproperlyConfigured('Every %s must have a slug.'
                                       % self.__class__)

    def __unicode__(self):
        name = getattr(self, 'name', u"Unnamed %s" % self.__class__.__name__)
        return unicode(name)

    def _get_default_urlpatterns(self):
        package_string = '.'.join(self.__module__.split('.')[:-1])
        if getattr(self, 'urls', None):
            try:
                mod = import_module('.%s' % self.urls, package_string)
            except ImportError:
                mod = import_module(self.urls)
            urlpatterns = mod.urlpatterns
        else:
            # Try importing a urls.py from the dashboard package
            if module_has_submodule(import_module(package_string), 'urls'):
                urls_mod = import_module('.urls', package_string)
                urlpatterns = urls_mod.urlpatterns
            else:
                urlpatterns = patterns('')
        return urlpatterns


class Registry(object):
    def __init__(self):
        self._registry = {}
        if not getattr(self, '_registerable_class', None):
            raise ImproperlyConfigured('Subclasses of Registry must set a '
                                       '"_registerable_class" property.')

    def _register(self, cls):
        """Registers the given class.

        If the specified class is already registered then it is ignored.
        """
        if not inspect.isclass(cls):
            raise ValueError('Only classes may be registered.')
        elif not issubclass(cls, self._registerable_class):
            raise ValueError('Only %s classes or subclasses may be registered.'
                             % self._registerable_class.__name__)

        if cls not in self._registry:
            cls._registered_with = self
            self._registry[cls] = cls()

        return self._registry[cls]

    def _unregister(self, cls):
        """Unregisters the given class.

        If the specified class isn't registered, ``NotRegistered`` will
        be raised.
        """
        if not issubclass(cls, self._registerable_class):
            raise ValueError('Only %s classes or subclasses may be '
                             'unregistered.' % self._registerable_class)

        if cls not in self._registry.keys():
            raise NotRegistered('%s is not registered' % cls)

        del self._registry[cls]

        return True

    def _registered(self, cls):
        if inspect.isclass(cls) and issubclass(cls, self._registerable_class):
            found = self._registry.get(cls, None)
            if found:
                return found
        else:
            # Allow for fetching by slugs as well.
            for registered in self._registry.values():
                if registered.slug == cls:
                    return registered
        class_name = self._registerable_class.__name__
        if hasattr(self, "_registered_with"):
            parent = self._registered_with._registerable_class.__name__
            raise NotRegistered('%(type)s with slug "%(slug)s" is not '
                                'registered with %(parent)s "%(name)s".'
                                    % {"type": class_name,
                                       "slug": cls,
                                       "parent": parent,
                                       "name": self.slug})
        else:
            slug = getattr(cls, "slug", cls)
            raise NotRegistered('%(type)s with slug "%(slug)s" is not '
                                'registered.' % {"type": class_name,
                                                 "slug": slug})


class Panel(HorizonComponent):
    """ A base class for defining Horizon dashboard panels.

    All Horizon dashboard panels should extend from this class. It provides
    the appropriate hooks for automatically constructing URLconfs, and
    providing role-based access control.

    .. attribute:: name

        The name of the panel. This will be displayed in the
        auto-generated navigation and various other places.
        Default: ``''``.

    .. attribute:: slug

        A unique "short name" for the panel. The slug is used as
        a component of the URL path for the panel. Default: ``''``.

    .. attribute: roles

        A list of role names, all of which a user must possess in order
        to access any view associated with this panel. This attribute
        is combined cumulatively with any roles required on the
        ``Dashboard`` class with which it is registered.

    .. attribute:: services

        A list of service names, all of which must be in the service catalog
        in order for this panel to be available.

    .. attribute:: urls

        Path to a URLconf of views for this panel using dotted Python
        notation. If no value is specified, a file called ``urls.py``
        living in the same package as the ``panel.py`` file is used.
        Default: ``None``.

    .. attribute:: nav
    .. method:: nav(context)

        The ``nav`` attribute can be either boolean value or a callable
        which accepts a ``RequestContext`` object as a single argument
        to control whether or not this panel should appear in
        automatically-generated navigation. Default: ``True``.

    .. attribute:: index_url_name

        The ``name`` argument for the URL pattern which corresponds to
        the index view for this ``Panel``. This is the view that
        :meth:`.Panel.get_absolute_url` will attempt to reverse.
    """
    name = ''
    slug = ''
    urls = None
    nav = True
    index_url_name = "index"

    def __repr__(self):
        return "<Panel: %s>" % self.slug

    def get_absolute_url(self):
        """ Returns the default URL for this panel.

        The default URL is defined as the URL pattern with ``name="index"`` in
        the URLconf for this panel.
        """
        try:
            return reverse('horizon:%s:%s:%s' % (self._registered_with.slug,
                                                 self.slug,
                                                 self.index_url_name))
        except:
            # Logging here since this will often be called in a template
            # where the exception would be hidden.
            LOG.exception("Error reversing absolute URL for %s." % self)
            raise

    @property
    def _decorated_urls(self):
        urlpatterns = self._get_default_urlpatterns()

        # Apply access controls to all views in the patterns
        roles = getattr(self, 'roles', [])
        services = getattr(self, 'services', [])
        _decorate_urlconf(urlpatterns, require_roles, roles)
        _decorate_urlconf(urlpatterns, require_services, services)
        _decorate_urlconf(urlpatterns, _current_component, panel=self)

        # Return the three arguments to django.conf.urls.defaults.include
        return urlpatterns, self.slug, self.slug


class PanelGroup(object):
    """ A container for a set of :class:`~horizon.Panel` classes.

    When iterated, it will yield each of the ``Panel`` instances it
    contains.

    .. attribute:: slug

        A unique string to identify this panel group. Required.

    .. attribute:: name

        A user-friendly name which will be used as the group heading in
        places such as the navigation. Default: ``None``.

    .. attribute:: panels

        A list of panel module names which should be contained within this
        grouping.
    """
    def __init__(self, dashboard, slug=None, name=None, panels=None):
        self.dashboard = dashboard
        self.slug = slug or getattr(self, "slug", "default")
        self.name = name or getattr(self, "name", None)
        # Our panels must be mutable so it can be extended by others.
        self.panels = list(panels or getattr(self, "panels", []))

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self.slug)

    def __unicode__(self):
        return self.name

    def __iter__(self):
        panel_instances = []
        for name in self.panels:
            try:
                panel_instances.append(self.dashboard.get_panel(name))
            except NotRegistered, e:
                LOG.debug(e)
        return iter(panel_instances)


class Dashboard(Registry, HorizonComponent):
    """ A base class for defining Horizon dashboards.

    All Horizon dashboards should extend from this base class. It provides the
    appropriate hooks for automatic discovery of :class:`~horizon.Panel`
    modules, automatically constructing URLconfs, and providing role-based
    access control.

    .. attribute:: name

        The name of the dashboard. This will be displayed in the
        auto-generated navigation and various other places.
        Default: ``''``.

    .. attribute:: slug

        A unique "short name" for the dashboard. The slug is used as
        a component of the URL path for the dashboard. Default: ``''``.

    .. attribute:: panels

        The ``panels`` attribute can be either a flat list containing the name
        of each panel **module**  which should be loaded as part of this
        dashboard, or a list of :class:`~horizon.PanelGroup` classes which
        define groups of panels as in the following example::

            class SystemPanels(horizon.PanelGroup):
                slug = "syspanel"
                name = _("System Panel")
                panels = ('overview', 'instances', ...)

            class Syspanel(horizon.Dashboard):
                panels = (SystemPanels,)

        Automatically generated navigation will use the order of the
        modules in this attribute.

        Default: ``[]``.

        .. warning::

            The values for this attribute should not correspond to the
            :attr:`~.Panel.name` attributes of the ``Panel`` classes.
            They should be the names of the Python modules in which the
            ``panel.py`` files live. This is used for the automatic
            loading and registration of ``Panel`` classes much like
            Django's ``ModelAdmin`` machinery.

            Panel modules must be listed in ``panels`` in order to be
            discovered by the automatic registration mechanism.

    .. attribute:: default_panel

        The name of the panel which should be treated as the default
        panel for the dashboard, i.e. when you visit the root URL
        for this dashboard, that's the panel that is displayed.
        Default: ``None``.

    .. attribute:: roles

        A list of role names, all of which a user must possess in order
        to access any panel registered with this dashboard. This attribute
        is combined cumulatively with any roles required on individual
        :class:`~horizon.Panel` classes.

    .. attribute:: services

        A list of service names, all of which must be in the service catalog
        in order for this dashboard to be available.

    .. attribute:: urls

        Optional path to a URLconf of additional views for this dashboard
        which are not connected to specific panels. Default: ``None``.

    .. attribute:: nav

        Optional boolean to control whether or not this dashboard should
        appear in automatically-generated navigation. Default: ``True``.

    .. attribute:: supports_tenants

        Optional boolean that indicates whether or not this dashboard includes
        support for projects/tenants. If set to ``True`` this dashboard's
        navigation will include a UI element that allows the user to select
        project/tenant. Default: ``False``.

    .. attribute:: public

        Boolean value to determine whether this dashboard can be viewed
        without being logged in. Defaults to ``False``.
    """
    _registerable_class = Panel
    name = ''
    slug = ''
    urls = None
    panels = []
    default_panel = None
    nav = True
    supports_tenants = False
    public = False

    def __repr__(self):
        return "<Dashboard: %s>" % self.slug

    def __init__(self, *args, **kwargs):
        super(Dashboard, self).__init__(*args, **kwargs)
        self._panel_groups = None

    def get_panel(self, panel):
        """
        Returns the specified :class:`~horizon.Panel` instance registered
        with this dashboard.
        """
        return self._registered(panel)

    def get_panels(self):
        """
        Returns the :class:`~horizon.Panel` instances registered with this
        dashboard in order, without any panel groupings.
        """
        all_panels = []
        panel_groups = self.get_panel_groups()
        for panel_group in panel_groups.values():
            all_panels.extend(panel_group)
        return all_panels

    def get_panel_group(self, slug):
        return self._panel_groups[slug]

    def get_panel_groups(self):
        registered = copy.copy(self._registry)
        panel_groups = []

        # Gather our known panels
        for panel_group in self._panel_groups.values():
            for panel in panel_group:
                registered.pop(panel.__class__)
            panel_groups.append((panel_group.slug, panel_group))

        # Deal with leftovers (such as add-on registrations)
        if len(registered):
            slugs = [panel.slug for panel in registered.values()]
            new_group = PanelGroup(self,
                                   slug="other",
                                   name=_("Other"),
                                   panels=slugs)
            panel_groups.append((new_group.slug, new_group))
        return SortedDict(panel_groups)

    def get_absolute_url(self):
        """ Returns the default URL for this dashboard.

        The default URL is defined as the URL pattern with ``name="index"``
        in the URLconf for the :class:`~horizon.Panel` specified by
        :attr:`~horizon.Dashboard.default_panel`.
        """
        try:
            return self._registered(self.default_panel).get_absolute_url()
        except:
            # Logging here since this will often be called in a template
            # where the exception would be hidden.
            LOG.exception("Error reversing absolute URL for %s." % self)
            raise

    @property
    def _decorated_urls(self):
        urlpatterns = self._get_default_urlpatterns()

        default_panel = None

        # Add in each panel's views except for the default view.
        for panel in self._registry.values():
            if panel.slug == self.default_panel:
                default_panel = panel
                continue
            urlpatterns += patterns('',
                    url(r'^%s/' % panel.slug, include(panel._decorated_urls)))
        # Now the default view, which should come last
        if not default_panel:
            raise NotRegistered('The default panel "%s" is not registered.'
                                % self.default_panel)
        urlpatterns += patterns('',
                url(r'', include(default_panel._decorated_urls)))

        # Require login if not public.
        if not self.public:
            _decorate_urlconf(urlpatterns, require_auth)
        # Apply access controls to all views in the patterns
        roles = getattr(self, 'roles', [])
        services = getattr(self, 'services', [])
        _decorate_urlconf(urlpatterns, require_roles, roles)
        _decorate_urlconf(urlpatterns, require_services, services)
        _decorate_urlconf(urlpatterns, _current_component, dashboard=self)

        # Return the three arguments to django.conf.urls.defaults.include
        return urlpatterns, self.slug, self.slug

    def _autodiscover(self):
        """ Discovers panels to register from the current dashboard module. """
        if getattr(self, "_autodiscover_complete", False):
            return

        panels_to_discover = []
        panel_groups = []
        # If we have a flat iterable of panel names, wrap it again so
        # we have a consistent structure for the next step.
        if all([isinstance(i, basestring) for i in self.panels]):
            self.panels = [self.panels]

        # Now iterate our panel sets.
        for panel_set in self.panels:
            # Instantiate PanelGroup classes.
            if not isinstance(panel_set, collections.Iterable) and \
                    issubclass(panel_set, PanelGroup):
                panel_group = panel_set(self)
            # Check for nested tuples, and convert them to PanelGroups
            elif not isinstance(panel_set, PanelGroup):
                panel_group = PanelGroup(self, panels=panel_set)

            # Put our results into their appropriate places
            panels_to_discover.extend(panel_group.panels)
            panel_groups.append((panel_group.slug, panel_group))

        self._panel_groups = SortedDict(panel_groups)

        # Do the actual discovery
        package = '.'.join(self.__module__.split('.')[:-1])
        mod = import_module(package)
        for panel in panels_to_discover:
            try:
                before_import_registry = copy.copy(self._registry)
                import_module('.%s.panel' % panel, package)
            except:
                self._registry = before_import_registry
                if module_has_submodule(mod, panel):
                    raise
        self._autodiscover_complete = True

    @classmethod
    def register(cls, panel):
        """ Registers a :class:`~horizon.Panel` with this dashboard. """
        return Horizon.register_panel(cls, panel)

    @classmethod
    def unregister(cls, panel):
        """ Unregisters a :class:`~horizon.Panel` from this dashboard. """
        return Horizon.unregister_panel(cls, panel)


class Workflow(object):
    def __init__(*args, **kwargs):
        raise NotImplementedError()


try:
    from django.utils.functional import empty
except ImportError:
    #Django 1.3 fallback
    empty = None


class LazyURLPattern(SimpleLazyObject):
    def __iter__(self):
        if self._wrapped is empty:
            self._setup()
        return iter(self._wrapped)

    def __reversed__(self):
        if self._wrapped is empty:
            self._setup()
        return reversed(self._wrapped)


class Site(Registry, HorizonComponent):
    """ The overarching class which encompasses all dashboards and panels. """

    # Required for registry
    _registerable_class = Dashboard

    name = "Horizon"
    namespace = 'horizon'
    slug = 'horizon'
    urls = 'horizon.site_urls'

    def __repr__(self):
        return u"<Site: %s>" % self.slug

    @property
    def _conf(self):
        conf = copy.copy(HORIZON_CONFIG)
        conf.update(getattr(settings, 'HORIZON_CONFIG', {}))
        return conf

    @property
    def dashboards(self):
        return self._conf['dashboards']

    @property
    def default_dashboard(self):
        return self._conf['default_dashboard']

    def register(self, dashboard):
        """ Registers a :class:`~horizon.Dashboard` with Horizon."""
        return self._register(dashboard)

    def unregister(self, dashboard):
        """ Unregisters a :class:`~horizon.Dashboard` from Horizon. """
        return self._unregister(dashboard)

    def registered(self, dashboard):
        return self._registered(dashboard)

    def register_panel(self, dashboard, panel):
        dash_instance = self.registered(dashboard)
        return dash_instance._register(panel)

    def unregister_panel(self, dashboard, panel):
        dash_instance = self.registered(dashboard)
        if not dash_instance:
            raise NotRegistered("The dashboard %s is not registered."
                                % dashboard)
        return dash_instance._unregister(panel)

    def get_dashboard(self, dashboard):
        """ Returns the specified :class:`~horizon.Dashboard` instance. """
        return self._registered(dashboard)

    def get_dashboards(self):
        """ Returns an ordered tuple of :class:`~horizon.Dashboard` modules.

        Orders dashboards according to the ``"dashboards"`` key in
        ``settings.HORIZON_CONFIG`` or else returns all registered dashboards
        in alphabetical order.

        Any remaining :class:`~horizon.Dashboard` classes registered with
        Horizon but not listed in ``settings.HORIZON_CONFIG['dashboards']``
        will be appended to the end of the list alphabetically.
        """
        if self.dashboards:
            registered = copy.copy(self._registry)
            dashboards = []
            for item in self.dashboards:
                dashboard = self._registered(item)
                dashboards.append(dashboard)
                registered.pop(dashboard.__class__)
            if len(registered):
                extra = registered.values()
                extra.sort()
                dashboards.extend(extra)
            return dashboards
        else:
            dashboards = self._registry.values()
            dashboards.sort()
            return dashboards

    def get_default_dashboard(self):
        """ Returns the default :class:`~horizon.Dashboard` instance.

        If ``"default_dashboard"`` is specified in ``settings.HORIZON_CONFIG``
        then that dashboard will be returned. If not, the first dashboard
        returned by :func:`~horizon.get_dashboards` will be returned.
        """
        if self.default_dashboard:
            return self._registered(self.default_dashboard)
        elif len(self._registry):
            return self.get_dashboards()[0]
        else:
            raise NotRegistered("No dashboard modules have been registered.")

    def get_user_home(self, user):
        """ Returns the default URL for a particular user.

        This method can be used to customize where a user is sent when
        they log in, etc. By default it returns the value of
        :meth:`get_absolute_url`.

        An alternative function can be supplied to customize this behavior
        by specifying a either a URL or a function which returns a URL via
        the ``"user_home"`` key in ``settings.HORIZON_CONFIG``. Each of these
        would be valid::

            {"user_home": "/home",}  # A URL
            {"user_home": "my_module.get_user_home",}  # Path to a function
            {"user_home": lambda user: "/" + user.name,}  # A function

        This can be useful if the default dashboard may not be accessible
        to all users.
        """
        user_home = self._conf['user_home']
        if user_home:
            if callable(user_home):
                return user_home(user)
            elif isinstance(user_home, basestring):
                # Assume we've got a URL if there's a slash in it
                if user_home.find("/") != -1:
                    return user_home
                else:
                    mod, func = user_home.rsplit(".", 1)
                    return getattr(import_module(mod), func)(user)
            # If it's not callable and not a string, it's wrong.
            raise ValueError('The user_home setting must be either a string '
                             'or a callable object (e.g. a function).')
        else:
            return self.get_absolute_url()

    def get_absolute_url(self):
        """ Returns the default URL for Horizon's URLconf.

        The default URL is determined by calling
        :meth:`~horizon.Dashboard.get_absolute_url`
        on the :class:`~horizon.Dashboard` instance returned by
        :meth:`~horizon.get_default_dashboard`.
        """
        return self.get_default_dashboard().get_absolute_url()

    @property
    def _lazy_urls(self):
        """ Lazy loading for URL patterns.

        This method avoids problems associated with attempting to evaluate
        the the URLconf before the settings module has been loaded.
        """
        def url_patterns():
            return self._urls()[0]

        return LazyURLPattern(url_patterns), self.namespace, self.slug

    def _urls(self):
        """ Constructs the URLconf for Horizon from registered Dashboards. """
        urlpatterns = self._get_default_urlpatterns()
        self._autodiscover()

        # Discover each dashboard's panels.
        for dash in self._registry.values():
            dash._autodiscover()

        # Allow for override modules
        config = getattr(settings, "HORIZON_CONFIG", {})
        if config.get("customization_module", None):
            customization_module = config["customization_module"]
            bits = customization_module.split('.')
            mod_name = bits.pop()
            package = '.'.join(bits)
            try:
                before_import_registry = copy.copy(self._registry)
                import_module('%s.%s' % (package, mod_name))
            except:
                self._registry = before_import_registry
                if module_has_submodule(package, mod_name):
                    raise

        # Compile the dynamic urlconf.
        for dash in self._registry.values():
            urlpatterns += patterns('',
                    url(r'^%s/' % dash.slug, include(dash._decorated_urls)))

        # Return the three arguments to django.conf.urls.defaults.include
        return urlpatterns, self.namespace, self.slug

    def _autodiscover(self):
        """ Discovers modules to register from ``settings.INSTALLED_APPS``.

        This makes sure that the appropriate modules get imported to register
        themselves with Horizon.
        """
        if not getattr(self, '_registerable_class', None):
            raise ImproperlyConfigured('You must set a '
                                       '"_registerable_class" property '
                                       'in order to use autodiscovery.')
        # Discover both dashboards and panels, in that order
        for mod_name in ('dashboard', 'panel'):
            for app in settings.INSTALLED_APPS:
                mod = import_module(app)
                try:
                    before_import_registry = copy.copy(self._registry)
                    import_module('%s.%s' % (app, mod_name))
                except:
                    self._registry = before_import_registry
                    if module_has_submodule(mod, mod_name):
                        raise


class HorizonSite(Site):
    """
    A singleton implementation of Site such that all dealings with horizon
    get the same instance no matter what. There can be only one.
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(Site, cls).__new__(cls, *args, **kwargs)
        return cls._instance


# The one true Horizon
Horizon = HorizonSite()
