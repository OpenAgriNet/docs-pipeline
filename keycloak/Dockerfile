FROM quay.io/keycloak/keycloak:26.1.4

COPY providers/keycloak-2fa-email-authenticator.jar \
     /opt/keycloak/providers/keycloak-2fa-email-authenticator.jar

COPY import/ /opt/keycloak/data/import/

USER keycloak
RUN /opt/keycloak/bin/kc.sh build
