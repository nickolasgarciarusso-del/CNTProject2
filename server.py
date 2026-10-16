"""
CNT4713 File Transfer Project - Server

Usage: python server.py <port>

Protocol (the client must follow this):

  CONTROL connection (TCP, port given on the command line)
    - Client -> server: one command per line, terminated by "\n"
        login <username>
        list
        stor <filename>
        retr <filename>
        dele <filename>
        quit
    - Server -> client responses:
        "<status code>\n\n"            (status code only)
        "<status code>\n\n<data>\n"    (only the connect reply carries data:
                                        the DATA PORT number)
      Status codes: 200 = success, 500 = failure.

  DATA connection (TCP, port sent by the server right after connecting)
    - Every transfer on the data connection is one framed message:
        8-byte unsigned big-endian length, followed by that many bytes.
    - list : server replies 200 on control, then sends the comma separated
             file list ("a.txt, b.txt") as one framed message on data.
    - stor : client sends "stor <filename>" on control, then the file
             contents as one framed message on data; server replies 200/500.
    - retr : server replies 200 on control and then sends the file contents
             as one framed message on data; on failure only 500 is sent.
    - dele : status code only.
"""

import os
import socket
import struct
import sys
import threading

# Directory the server stores, lists, sends and deletes files in.
FILE_DIR = os.getcwd()

LENGTH_HEADER = struct.Struct("!Q")


def send_response(conn, code, data=None):
    message = f"{code}\n\n"
    if data is not None:
        message += f"{data}\n"
    conn.sendall(message.encode())


def recv_exact(sock, size):
    chunks = []
    while size > 0:
        chunk = sock.recv(min(size, 65536))
        if not chunk:
            raise ConnectionError("data connection closed")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def send_data(sock, payload):
    sock.sendall(LENGTH_HEADER.pack(len(payload)) + payload)


def recv_data(sock):
    (size,) = LENGTH_HEADER.unpack(recv_exact(sock, LENGTH_HEADER.size))
    return recv_exact(sock, size)


def safe_path(filename):
    """Return the path of filename inside FILE_DIR, or None if invalid."""
    name = os.path.basename(filename)
    if not name or name in (".", ".."):
        return None
    return os.path.join(FILE_DIR, name)


class ClientSession:
    def __init__(self, control_conn):
        self.control = control_conn
        self.username = None
        self.data_listener = None
        self.data_conn = None

    def open_data_socket(self):
        print("Connection requested. Creating data socket", flush=True)
        self.data_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.data_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.data_listener.bind(("", 0))
        self.data_listener.listen(1)
        return self.data_listener.getsockname()[1]

    def get_data_conn(self):
        """Accept the client's data connection the first time it is needed."""
        if self.data_conn is None:
            self.data_conn, _ = self.data_listener.accept()
            print("Connection requested. Creating data socket", flush=True)
        return self.data_conn

    def run(self):
        try:
            data_port = self.open_data_socket()
            send_response(self.control, 200, data_port)

            reader = self.control.makefile("r", encoding="utf-8", newline="\n")
            for line in reader:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(maxsplit=1)
                command = parts[0].lower()
                argument = parts[1].strip() if len(parts) > 1 else ""

                if command == "quit":
                    self.handle_quit()
                    break
                self.dispatch(command, argument)
        except (ConnectionError, OSError):
            pass
        finally:
            self.close()

    def dispatch(self, command, argument):
        if command == "login":
            self.handle_login(argument)
            return
        if self.username is None:
            send_response(self.control, 500)
            return

        handlers = {
            "list": self.handle_list,
            "stor": self.handle_stor,
            "retr": self.handle_retr,
            "dele": self.handle_dele,
        }
        handler = handlers.get(command)
        if handler is None:
            send_response(self.control, 500)
            return
        if command in ("list", "stor", "retr"):
            self.get_data_conn()
        if command == "list":
            handler()
        else:
            handler(argument)

    def handle_login(self, username):
        if not username or " " in username:
            send_response(self.control, 500)
            return
        print(f"Login requested by: {username}", flush=True)
        self.username = username
        send_response(self.control, 200)

    def handle_list(self):
        print(f"List requested by {self.username}. Sending files.", flush=True)
        try:
            files = sorted(
                name for name in os.listdir(FILE_DIR)
                if os.path.isfile(os.path.join(FILE_DIR, name))
            )
        except OSError:
            send_response(self.control, 500)
            return
        data_conn = self.get_data_conn()
        send_response(self.control, 200)
        send_data(data_conn, ", ".join(files).encode())

    def handle_stor(self, filename):
        print(f"Stor {filename} requested by {self.username}", flush=True)
        path = safe_path(filename)
        if path is None:
            send_response(self.control, 500)
            return
        # Always read the incoming data so the data stream stays in sync.
        contents = recv_data(self.get_data_conn())
        try:
            with open(path, "wb") as f:
                f.write(contents)
        except OSError:
            send_response(self.control, 500)
            return
        print("STOR complete", flush=True)
        send_response(self.control, 200)

    def handle_retr(self, filename):
        print(f"Retr requested by {self.username}. Sending file: {filename}", flush=True)
        path = safe_path(filename)
        try:
            if path is None:
                raise OSError("invalid filename")
            with open(path, "rb") as f:
                contents = f.read()
        except OSError:
            send_response(self.control, 500)
            return
        data_conn = self.get_data_conn()
        send_response(self.control, 200)
        send_data(data_conn, contents)
        print("File sent.", flush=True)

    def handle_dele(self, filename):
        print(f"Dele requested by {self.username}. Deleting file: {filename}", flush=True)
        path = safe_path(filename)
        try:
            if path is None or not os.path.isfile(path):
                raise OSError("file not found")
            os.remove(path)
        except OSError:
            send_response(self.control, 500)
            return
        print("Delete complete", flush=True)
        send_response(self.control, 200)

    def handle_quit(self):
        print(f"Quit requested by {self.username}", flush=True)
        send_response(self.control, 200)

    def close(self):
        for sock in (self.data_conn, self.data_listener, self.control):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def main():
    if len(sys.argv) != 2:
        print("Usage: python server.py <port>")
        sys.exit(1)
    port = int(sys.argv[1])

    print("Starting server…", flush=True)
    print("Creating server socket", flush=True)
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("", port))
    server_sock.listen(5)
    print("Awaiting connections…", flush=True)

    try:
        while True:
            conn, _ = server_sock.accept()
            session = ClientSession(conn)
            threading.Thread(target=session.run, daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        server_sock.close()


if __name__ == "__main__":
    main()
