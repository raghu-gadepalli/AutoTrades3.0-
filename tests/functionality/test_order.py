# from services.oms.oms_client_service import get_active_clients ---Refactored and removed
from services.oms.oms_kite_service import get_kite_client_by_id
# from services.oms.oms_order_service import upsert_order ---Refactored and removed


def poll_orders():

    clients = get_active_clients()

    for client in clients:
        print ( "Client : ", client );
        try:

            kite = get_kite_client_by_id(client)

            orders = kite.orders()
            
            for order in orders:
                print ( order );
                print ( "\n" );

        except Exception as e:

            print(f"Order polling error for {client}: {e}")

def main():

    print("Starting Orders poller...")

    poll_orders()


if __name__ == "__main__":
    main()                