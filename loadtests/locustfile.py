from locust import HttpUser, between, task


class MwanachuoApiUser(HttpUser):
    wait_time = between(1, 3)

    @task(5)
    def list_restaurants(self):
        self.client.get("/api/restaurants/?category=all&limit=20&offset=0", name="GET /api/restaurants")

    @task(3)
    def list_restaurants_geo(self):
        self.client.get("/api/restaurants/?category=all&lat=-6.7924&lng=39.2083&limit=20&offset=0", name="GET /api/restaurants geo")

    @task(2)
    def list_available_jobs(self):
        self.client.get("/api/orders/available-jobs/?limit=20&offset=0", name="GET /api/orders/available-jobs")

