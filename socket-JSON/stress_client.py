import socket
import threading
import json
import struct
import sys
import time
import random
from collections import defaultdict

# --- Helper functions (copied from socket_client.py for consistency) ---
MAX_MESSAGE_SIZE = 1024 * 1024  # 1MB maximum message size to avoid misuse

def send_json(sock, data):
    try:
        json_bytes = json.dumps(data).encode('utf-8')
        length = struct.pack('!I', len(json_bytes))
        sock.sendall(length + json_bytes)
        return True
    except Exception:
        return False

def recv_exact(sock, n):
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data

def recv_json(sock):
    try:
        length_bytes = recv_exact(sock, 4)
        if not length_bytes:
            return None
        length = struct.unpack('!I', length_bytes)[0]
        
        if length > MAX_MESSAGE_SIZE:
            return None # Server will likely close connection
        
        json_bytes = recv_exact(sock, length)
        if not json_bytes:
            return None
        return json.loads(json_bytes.decode('utf-8'))
    except Exception:
        return None
# --- End Helper functions ---

class ClientState:
    def __init__(self):
        self.active_clients = set()
        self.client_id = None
        self.ready = threading.Event()
        self.lock = threading.Lock() # Protects active_clients and client_id

def stress_client_thread(host, port, client_idx, message_count, message_interval):
    """
    Function run by each client thread to connect and send messages.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_state = ClientState()
    connected = False

    try:
        sock.connect((host, port))
        print(f"[Client {client_idx}] Connecting to {host}:{port}...")
        connected = True

        # Start a receiver thread for this client
        def client_receiver():
            while True:
                msg = recv_json(sock)
                if msg is None:
                    print(f"[Client {client_idx}] Server disconnected.")
                    break
                
                with client_state.lock:
                    if msg.get("type") == "welcome":
                        client_state.client_id = msg.get("client_id")
                        client_state.active_clients.add(client_state.client_id)
                        client_state.ready.set()
                        print(f"[Client {client_idx} (ID: {client_state.client_id})] Connected!")
                    elif msg.get("type") == "list":
                        # Update our local view of active clients (keep as ints to match server IDs)
                        current_clients = set(msg.get("clients", []))
                        client_state.active_clients = current_clients
                        print(f"[Client {client_idx} (ID: {client_state.client_id})] Received: list -> {sorted(list(client_state.active_clients))}")
                        # print(f"[Client {client_idx} (ID: {client_state.client_id})] Active clients updated: {list(client_state.active_clients)}")
                    elif msg.get("type") == "message":
                        from_id = msg.get("from")
                        content = msg.get("message")
                        print(f"[Client {client_idx} (ID: {client_state.client_id})] Received: message from {from_id}")
                        # print(f"[Client {client_idx} (ID: {client_state.client_id})] Received from {from_id}: {content[:20]}...")
                    elif msg.get("type") == "history":
                        target_id = msg.get("target_id")
                        messages = msg.get("messages")
                        print(f"[Client {client_idx} (ID: {client_state.client_id})] Received: history with {target_id} -> {len(messages)} msgs")
                        # print(f"[Client {client_idx} (ID: {client_state.client_id})] Received history with {target_id}: {len(messages)} messages.")
                    elif msg.get("type") == "success":
                        print(f"[Client {client_idx} (ID: {client_state.client_id})] Received: success")
                    elif msg.get("type") == "error":
                        print(f"[Client {client_idx} (ID: {client_state.client_id})] Server Error: {msg.get('message')}")
                    # else:
                    #     print(f"[Client {client_idx} (ID: {client_state.client_id})] Unhandled msg: {msg.get('type')}")

        receiver_thread = threading.Thread(target=client_receiver, daemon=True)
        receiver_thread.start()

        # Wait for welcome message
        if not client_state.ready.wait(timeout=5):
            print(f"[Client {client_idx}] Failed to receive welcome message. Exiting.")
            return
        
        print(f"[Client {client_idx} (ID: {client_state.client_id})] Connected! Starting message flood...")
        # Immediately request an initial client list to populate state
        print(f"[Client {client_idx} (ID: {client_state.client_id})] Sending: list")
        send_json(sock, {"command": "list"})
        time.sleep(0.1)

        for i in range(message_count):
            # Periodically request list of clients
            if i % 10 == 0: # Every 10 messages, refresh client list and request history
                print(f"[Client {client_idx} (ID: {client_state.client_id})] Sending: list")
                send_json(sock, {"command": "list"})
                time.sleep(0.05) # Small delay to let list response arrive

                with client_state.lock:
                    if len(client_state.active_clients) > 1 and client_state.client_id:
                        # Pick a random peer for history request
                        other_clients = list(client_state.active_clients - {client_state.client_id})
                        if other_clients:
                            history_target_id = random.choice(other_clients)
                            print(f"[Client {client_idx} (ID: {client_state.client_id})] Sending: history {history_target_id}")
                            send_json(sock, {"command": "history", "target_id": str(history_target_id)})
                            time.sleep(0.05)


            # Choose a target client for forwarding
            target_id_for_forward = None
            with client_state.lock:
                if len(client_state.active_clients) > 1 and client_state.client_id:
                    # Get a list of active clients *excluding* self
                    other_clients = list(client_state.active_clients - {client_state.client_id})
                    if other_clients:
                        target_id_for_forward = random.choice(other_clients)
                if not target_id_for_forward and client_state.client_id: # Fallback to self if no other clients or not assigned ID yet
                    target_id_for_forward = client_state.client_id

            if not target_id_for_forward:
                print(f"[Client {client_idx} (ID: {client_state.client_id})] No valid target for forward, skipping message {i+1}.")
                time.sleep(message_interval) # Still respect interval
                continue

            message_content = f"Hello from {client_state.client_id}! Message {i+1}."
            # Optionally add varied message sizes: message_content = "A" * random.randint(50, 500)

            send_data = {
                "command": "forward",
                "target_id": str(target_id_for_forward),
                "message": message_content
            }
            print(f"[Client {client_idx} (ID: {client_state.client_id})] Sending: forward -> {target_id_for_forward}")
            if not send_json(sock, send_data):
                print(f"[Client {client_idx} (ID: {client_state.client_id})] Send failed for message {i+1}. Disconnecting.")
                break
            # print(f"[Client {client_idx} (ID: {client_state.client_id})] Sent message {i+1} to {target_id_for_forward}")
            time.sleep(message_interval)

    except ConnectionRefusedError:
        print(f"[Client {client_idx}] Connection refused. Is server running on {host}:{port}?")
    except Exception as e:
        print(f"[Client {client_idx}] An error occurred: {e}")
    finally:
        if connected:
            send_json(sock, {"command": "exit"}) # Attempt graceful exit
            sock.close()
            print(f"[Client {client_idx}] Disconnected.")

def main():
    if len(sys.argv) < 4:
        print("Usage: python stress_client.py <HOST> <PORT> <NUM_CLIENTS> [MESSAGES_PER_CLIENT] [MESSAGE_INTERVAL_SECONDS]")
        print("Example: python stress_client.py 10.128.0.2 9999 50 100 0.1")
        sys.exit(1)

    host = sys.argv[1]
    port = int(sys.argv[2])
    num_clients = int(sys.argv[3])
    messages_per_client = int(sys.argv[4]) if len(sys.argv) > 4 else 50
    message_interval = float(sys.argv[5]) if len(sys.argv) > 5 else 0.05 # seconds

    print(f"Starting stress test with {num_clients} clients connecting to {host}:{port}")
    print(f"Each client will send {messages_per_client} messages with {message_interval}s interval.")

    threads = []
    for i in range(num_clients):
        t = threading.Thread(target=stress_client_thread, args=(host, port, i + 1, messages_per_client, message_interval))
        threads.append(t)
        t.start()
        time.sleep(0.01) # Small delay between starting clients

    for t in threads:
        t.join()

    print("Stress test finished.")

if __name__ == '__main__':
    main()
