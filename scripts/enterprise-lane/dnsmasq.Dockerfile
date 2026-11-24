# The lane's "company DNS": internal names (*.entlane.internal) answer with the private registry
# address; everything else is forwarded to the VM's own resolver.
FROM alpine:3.20
RUN apk add --no-cache dnsmasq
ENTRYPOINT ["dnsmasq", "-k", "--log-facility=-"]
