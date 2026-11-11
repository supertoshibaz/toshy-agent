"""Apply managed LSP paths without enabling a component that failed its startup."""


def configure(conf, defaults, executables):
    servers = conf.setdefault('servers', {})
    for name, executable in executables.items():
        server = servers.setdefault(name, dict(defaults['servers'][name]))
        if executable is None:
            server['enabled'] = False
            server['_toshy_install_failed'] = True
        else:
            server.update(command=executable, command_windows=executable)
            if server.pop('_toshy_install_failed', False):
                server['enabled'] = True
