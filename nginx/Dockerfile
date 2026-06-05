FROM nginx:1.27-alpine

COPY init-ssl.sh /docker-entrypoint.d/init-ssl.sh
RUN chmod +x /docker-entrypoint.d/init-ssl.sh

RUN mkdir -p /var/www/certbot
