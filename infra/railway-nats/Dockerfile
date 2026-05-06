FROM nats:2.10.29-alpine

COPY infra/railway-nats/start-nats.sh /usr/local/bin/start-nats.sh
RUN chmod 0755 /usr/local/bin/start-nats.sh

CMD ["/usr/local/bin/start-nats.sh"]
