from db.connection import ConnectionManager
import select
import psycopg2

cm = ConnectionManager()

def main():
    print("Establishing live LISTEN connection to the database...")
    try:
        conn = cm.get_connection()
        # LISTEN requires autocommit or committing after the LISTEN command
        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        
        with conn.cursor() as cur:
            # Subscribe to the 'new_tick' channel
            cur.execute("LISTEN new_tick;")
            print("Successfully subscribed to 'new_tick' channel!")
            print("Waiting for PostgreSQL NOTIFY events. Press Ctrl+C to exit.\n")
            
            while True:
                # select.select waits until there is I/O on the connection descriptor
                # 5 is the timeout in seconds
                if select.select([conn], [], [], 5) == ([], [], []):
                    # Timeout reached, no new notifications. You could do other work here.
                    pass
                else:
                    # There is I/O ready, let's poll the connection
                    conn.poll()
                    # Process all pending notifications
                    while conn.notifies:
                        notify = conn.notifies.pop(0)
                        print(f"[{notify.pid}] Received TICK on {notify.channel}: {notify.payload}")
                        
    except KeyboardInterrupt:
        print("\nClosing connection...")
    except Exception as e:
        print(f"\nDatabase error: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()
            print("Connection successfully closed.")

if __name__ == "__main__":
    main()
