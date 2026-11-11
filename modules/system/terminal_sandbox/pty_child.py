"""Become the controlling-terminal process before executing the requested shell."""
import os
import sys


def main():
    import fcntl
    import termios
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)


if __name__ == '__main__':
    main()
