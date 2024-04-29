# npipe_socket_adapter.py

A Python script to convert both ways between Windows named pipes and UNIX domain sockets, for use within WSL 2 (Windows subsystem for Linux).

## Usage

Download `npipe_socket_adapter.py` and place it in your WSL 2. The scripts requires a Powershell installation on the Windows host, which is used to serve/connect to named pipes on the Windows side. The Powershell installation that ships with Windows 10/11 is sufficient for this.

### Examples
Expose an existing UNIX domain socket from WSL 2 as a named pipe on the Windows host:
```sh
$ ./npipe_socket_adapter socket-to-npipe -s my_socket -n my_named_pipe
```

Expose an existing named pipe on the Windows host as a UNIX domain socket:
```sh
$ ./npipe_socket_adapter npipe-to-socket -s my_socket -n my_named_pipe
```
Once started, the script will serve either a named pipe or a UNIX domain socket, until it is stopped by `Ctrl+C` or `kill <pid>`.

Note that the named pipes must not include the `\\.\pipe\` prefix.

### Command-line interface
```
usage: npipe_socket_adapter.py [-h] --npipe NPIPE --socket SOCKET [-p POWERSHELL] [--verbose] {npipe-to-socket,socket-to-npipe}

Adapter to convert between Windows named pipes and UNIX domain sockets, in the context of WSL 2.

positional arguments:
  {npipe-to-socket,socket-to-npipe}
                        Whether to expose an existing Windows named pipe as a UNIX domain socket (npipe-to-socket), or to expose an existig UNIX domain socket as a Windows named pipe (socket-to-npipe)

optional arguments:
  -h, --help            show this help message and exit
  --npipe NPIPE, -n NPIPE
                        Name of the named pipe.
  --socket SOCKET, -s SOCKET
                        Name of the UNIX domain socket.
  -p POWERSHELL, --powershell POWERSHELL
                        Location of the Powershell executable to use. Powershell is used to convert a connection to a named pipe to standard IO streams.
  --verbose, -v         Enable verbose output.
```
