from restaurant.models import Orders
from .events import emit

def assign_driver(order, driver):

    order.driver = driver
    order.save()


    emit(
        "driver.assigned",
        order.id,
        {'driver_id': str(driver.id)}
    )