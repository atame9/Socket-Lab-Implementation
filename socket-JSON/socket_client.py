# Threaded TCP chat client that exchanges framed JSON messages with the server.
# The client connects, waits for a welcome message containing its assigned client_id,
# then allows the user to issue commands (list, forward, history, exit).

import socket
import threading
import json
import struct
import os

client_id = None #this is set by the server in the initial welcome message
ready_event = threading.Event() #this is used to block the prompt until welcome from server is received
goodbye_event = threading.Event()

MAX_MESSAGE_SIZE = 1024 * 1024  # 1MB maximum message size to avoid misuse

def send_json(sock, data):
    """Send JSON message with 4-byte length prefix."""
    try:
        json_bytes = json.dumps(data).encode('utf-8')
        length = struct.pack('!I', len(json_bytes)) #length prefix for the JSON payload
        sock.sendall(length + json_bytes) #send the length prefix and the JSON payload
        return True
    except Exception as e:
        print(f"\nSend error: {e}") #if there is an error, print the error
        return False
    
def recv_exact(sock, n):
    """Receive exactly n bytes. Handles partial receives."""
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data

def recv_json(sock):
    """Receive JSON message with 4-byte length prefix."""
    try:
        length_bytes = recv_exact(sock, 4) #receive the length prefix
        if not length_bytes:
            return None
        length = struct.unpack('!I', length_bytes)[0] #unpack the length prefix
        
        if length > MAX_MESSAGE_SIZE:
            print("\nMessage too large") #if the message is too large, print an error message
            return None
        
        json_bytes = recv_exact(sock, length) #receive the JSON payload
        if not json_bytes:
            return None
        return json.loads(json_bytes.decode('utf-8')) #decode the JSON payload
    except Exception as e:
        print(f"\nReceive error: {e}") #if there is an error, print the error
        return None
    
def receive_messages(sock):
    """Background thread: continuously receive and display server messages."""
    global client_id #need to modify the global variable client_id here
    
    while True:
        msg = recv_json(sock) #receive the message from the server
        if msg is None:
            print("\n[Server disconnected]") #if the message is None, print an error message
            break #break the loop if the message is None
        
        msg_type = msg.get("type") #get the type of the message
        
        if msg_type == "welcome":
            client_id = msg.get("client_id") #get the client_id from the message
            print(f'Connected! Your ID is: {client_id}') #print the client_id
            ready_event.set() #remove the block on the main thread
        
        elif msg_type == "message":
            from_id = msg.get("from") #get the from_id from the message
            content = msg.get("message") #get the content from the message
            timestamp = msg.get("timestamp", "") #get the timestamp from the message
            print(f'\n[{timestamp}] From {from_id}: {content}') #print the message
            print('> ', end='', flush=True) #print the prompt
        
        elif msg_type == "list":
            clients = msg.get("clients") #get the clients from the message
            print(f'\nActive clients: {", ".join(map(str, clients))}') #print the clients
            print('> ', end='', flush=True) #print the prompt
        
        elif msg_type == "history":
            target_id = msg.get("target_id") #get the target_id from the message
            messages = msg.get("messages", []) #get the messages from the message
            count = msg.get("count", 0) #get the count from the message
            custom_msg = msg.get("message") #get optional custom message
            
            print(f'\n--- History with Client {target_id} ({count} messages) ---')
            
            if custom_msg:
                # Display custom message (e.g., "No chat history with client X")
                print(custom_msg)
            elif messages:
                # Display message history
                for m in messages:
                    ts = m.get("timestamp", "")
                    print(f'[{ts}] {m["from"]} -> {m["to"]}: {m["message"]}')
            
            print('--- End ---')
            print('> ', end='', flush=True)
            
        elif msg_type == "success":
            print('\n✓ Message sent') #print the message
            print('> ', end='', flush=True) #print the prompt
        
        elif msg_type == "error":
            print(f'\n✗ Error: {msg.get("message")}') #print the error message
            print('> ', end='', flush=True) #print the prompt
        
        elif msg_type == "goodbye":
            print('\nGoodbye!') #print the goodbye message
            goodbye_event.set()
            break #break the loop if the message is None
        
def main():
    """Initializes and runs the multi-threaded chat client."""
    # Retrieve host and port from environment variables, with defaults.
    host = os.getenv('SERVER_HOST', '127.0.0.1')
    port = int(os.getenv('SERVER_PORT', 9999))

     # Default parameters create an IPv4 TCP socket
    sock = socket.socket()

    try:
        sock.connect((host, port))
        print(f'Connecting to {host}:{port}...') #print the connection message
    except ConnectionRefusedError:
        print(f'Connection refused. Is server running on {host}:{port}?') #print the connection refused message
        return
    except Exception as e:
        print(f'Connection error: {e}') #print the connection error message
        return
    
    # Start a background thread to continuously receive and render server messages
    receiver = threading.Thread(target=receive_messages, args=(sock,))
    receiver.daemon = False
    receiver.start()
    
    # Wait until the welcome message arrives so we know our client_id
    if not ready_event.wait(timeout=5):
        print("Timeout waiting for server welcome") #print the timeout message
        sock.shutdown(socket.SHUT_RDWR)
        sock.close()
        return
    
    print("\nCommands:") #print the commands
    print("  list")
    print("  forward <ID> <message>")
    print("  history <ID>")
    print("  exit\n")
    
    # Main loop: parse user input and send corresponding commands as JSON
    try:
        while receiver.is_alive(): #while the receiver is alive, get the input from the user
            inp = input('> ').strip()
            
            if not inp:
                continue #continue the loop if the input is empty
            
            parts = inp.split(maxsplit=2) #split the input into parts
            command = parts[0].lower()
            
            if command == "exit":
                send_json(sock, {"command": "exit"})
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                goodbye_event.wait(timeout=1.0)
                break #break the loop if the command is exit
            
            elif command == "list":
                send_json(sock, {"command": "list"}) 
            
            elif command == "forward":
                if len(parts) < 3:
                    print("Usage: forward <ID> <message>") #print the usage message
                    continue
                
                # Target is a client_id assigned by the server. Message is free text.
                send_json(sock, {
                    "command": "forward",
                    "target_id": parts[1],
                    "message": parts[2]
                })
            
            elif command == "history":
                if len(parts) < 2:
                    print("Usage: history <ID>") #print the usage message
                    continue
                
                send_json(sock, {
                    "command": "history",
                    "target_id": parts[1]
                })
            
            else:
                print(f"Unknown command: {command}") #print the unknown command message
    
    except KeyboardInterrupt: #if the user interrupts the program, print the disconnecting message
        print("\n\nDisconnecting...")
        send_json(sock, {"command": "exit"})
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        goodbye_event.wait(timeout=1.0)
    except EOFError: #if the user ends the program, print the disconnecting message
        print("\nEOF detected, disconnecting...")
        send_json(sock, {"command": "exit"})
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        goodbye_event.wait(timeout=1.0)
    finally: #finally, close the socket
        try:
            receiver.join(timeout=1.0)
        except Exception:
            pass
        sock.close()


if __name__ == '__main__':
    main()