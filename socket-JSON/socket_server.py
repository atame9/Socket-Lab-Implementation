# Threaded TCP server that assigns incremental client IDs and relays messages
# between clients. All application data is JSON framed with a 4-byte length
# prefix (big-endian). The server also keeps a bounded per-pair message history.

import socket
import threading
import json
import struct
from datetime import datetime

# Global variables
clients = {} # {client_id: connection_socket}
client_counter = 0 # Increasing counter for assigning client IDs
counter_lock = threading.Lock() # Lock for client_counter increments
clients_lock = threading.Lock() # Lock for the clients mapping
history = {} # {(id1, id2): [messages]} stores conversation transcript for client pairs
history_lock = threading.Lock() # Lock for history updates and reads

MAX_HISTORY_PER_PAIR = 100 # Maximum number of messages to store for each client pair
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
    
def get_history_key(id1, id2):
    """Return a sorted tuple to ensure same key regardless of order."""
    return tuple(sorted([id1, id2]))

def add_to_history(source_id, target_id, message):
    """Add message to chat history between two clients."""
    key = get_history_key(source_id, target_id) #get the key for the history
    with history_lock: #need thread safe access to the history
        if key not in history: #if the key is not in the history, create a new list
            history[key] = []
        
        history[key].append({ #add the message to the history
            "from": source_id,
            "to": target_id,
            "message": message,
            "timestamp": datetime.now().isoformat()
        })
        
        # Limit history size per pair to prevent unbounded memory usage.
        if len(history[key]) > MAX_HISTORY_PER_PAIR:
            history[key] = history[key][-MAX_HISTORY_PER_PAIR:]

def get_history(source_id, target_id):
    """Retrieve chat history between two clients."""
    key = get_history_key(source_id, target_id)
    with history_lock: #need thread safe access to the history
        return history.get(key, [])
    
def handle_client(conn, client_id):
    """Handle all communication with a single client."""
    print(f'Client {client_id} connected')
    
    try:
        # Send welcome message with assigned ID
        send_json(conn, {
            "type": "welcome",
            "client_id": client_id
        })
        
        while True:
            data = recv_json(conn) #receive the data from the client
            if data is None:
                break #break the loop if the data is None
            
            command = data.get("command", "").lower() #get the command from the data
            
            # Handle EXIT command
            if command == "exit":
                send_json(conn, {"type": "goodbye"})
                break
            
            # Handle LIST command
            elif command == "list":
                with clients_lock: #need thread safe access to the clients
                    active_ids = list(clients.keys()) #get the active ids from the clients
                send_json(conn, {
                    "type": "list",
                    "clients": active_ids
                })
            
            # Handle FORWARD command
            elif command == "forward":
                target_id = data.get("target_id")
                message = data.get("message")
                
                if not target_id or not message: #if the target_id or message is None, send an error message
                    send_json(conn, {
                        "type": "error",
                        "message": "Missing target_id or message"
                    })
                    continue
                
                try:
                    target_id = int(target_id)
                except ValueError: #if the target_id is not a number, send an error message
                    send_json(conn, {
                        "type": "error",
                        "message": "target_id must be a number"
                    })
                    continue
                
                # Forward message to target client
                with clients_lock: #need thread safe access to the clients
                    if target_id in clients:
                        forward_msg = {
                            "type": "message",
                            "from": client_id,
                            "message": message,
                            "timestamp": datetime.now().isoformat()
                        }
                        
                        if send_json(clients[target_id], forward_msg):
                            # Only save to history if send succeeded
                            add_to_history(client_id, target_id, message)
                            send_json(conn, {"type": "success"})
                        else:
                            send_json(conn, {
                                "type": "error",
                                "message": "Failed to deliver message"
                            })
                    else: #if the target_id is not found, send an error message
                        send_json(conn, {
                            "type": "error",
                            "message": f"Client {target_id} not found"
                        })
            
            # Handle HISTORY command
            elif command == "history":
                target_id = data.get("target_id")
                
                if not target_id:
                    send_json(conn, {
                        "type": "error",
                        "message": "Missing target_id"
                    })
                    continue
                
                try:
                    target_id = int(target_id)
                except ValueError: #if the target_id is not a number, send an error message
                    send_json(conn, {
                        "type": "error",
                        "message": "target_id must be a number"
                    })
                    continue
                
                # Check if target_id is valid (not greater than highest assigned ID)
                if target_id > client_counter:
                    send_json(conn, {
                        "type": "error",
                        "message": f"Client {target_id} does not exist (highest ID: {client_counter})"
                    })
                    continue
                
                messages = get_history(client_id, target_id)
                
                # Build response with helpful message when empty
                response = {
                    "type": "history",
                    "target_id": target_id,
                    "messages": messages,
                    "count": len(messages)
                }
                
                if not messages:
                    # Check if target is currently online
                    with clients_lock:
                        target_online = target_id in clients
                    
                    if target_online:
                        response["message"] = f"No chat history with client {target_id} yet (currently online)"
                    else:
                        response["message"] = f"No chat history with client {target_id} (client offline)"
                
                send_json(conn, response)
            
            # Unknown command
            else:
                send_json(conn, {
                    "type": "error",
                    "message": f"Unknown command: {command}"
                })
    
    except Exception as e:
        print(f'Error with client {client_id}: {e}') #if there is an error, print the error
    
    finally:
        # Cleanup: remove client from active list
        with clients_lock: #need thread safe access to the clients
            if client_id in clients:
                del clients[client_id]
        conn.close() #close the connection
        print(f'Client {client_id} disconnected') #print the disconnected message

def main():
    global client_counter #global variable for the client counter
    
    # Server configuration: bind to all interfaces so clients on the LAN can connect.
    HOST = '0.0.0.0'  # Listen on all interfaces
    PORT = 9999
    
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST, PORT))
    server_socket.listen(5)
    
    print(f'Server listening on {HOST}:{PORT}')
    
    try:
        while True:
            conn, addr = server_socket.accept()
            
            # Assign unique ID
            with counter_lock: #need thread safe access to the client counter
                client_counter += 1
                client_id = client_counter
            
            # Register client
            with clients_lock:
                clients[client_id] = conn
            
            print(f'New connection from {addr[0]}:{addr[1]} -> ID {client_id}')
            
            # Handle client in separate thread
            t = threading.Thread(target=handle_client, args=(conn, client_id))
            t.daemon = True #allows the main program to exit even if this thread is running
            t.start() #start the thread
    
    except KeyboardInterrupt:
        print('\nShutting down server...') #print the shutting down message
    finally:
        server_socket.close() #close the server socket


if __name__ == '__main__':
    main()