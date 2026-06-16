from domain.fsm.driver_fsm import DriverFSM


def release_driver(driver, decrement_count=1):
    driver.current_active_orders = max(0, driver.current_active_orders - decrement_count)
    if driver.current_active_orders == 0 and driver.status == 'DELIVERING':
        driver.status = DriverFSM.transition(driver.status, 'AVAILABLE')
    driver.save(update_fields=['current_active_orders', 'status'])
