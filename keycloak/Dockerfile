# Keycloak image with the email-OTP 2FA plugin pre-installed.
#
# Multi-stage build:
#   Stage 1 (alpine)   — downloads the plugin JAR via wget (no curl needed)
#   Stage 2 (keycloak) — copies the JAR in, runs kc.sh build to bake it in
#
# Nothing is baked into the image at build time.
# All SMTP / admin / realm config is injected at runtime via docker-compose env.
#
# Plugin : mesutpiskin/keycloak-2fa-email-authenticator v26.4.3 (KC 26.x)
# Base   : quay.io/keycloak/keycloak:26.1.4

# ── Stage 1: lightweight downloader ──────────────────────────────────────────
FROM alpine:3.19 AS downloader

ARG EMAIL_OTP_JAR_URL=https://github.com/mesutpiskin/keycloak-2fa-email-authenticator/releases/download/v26.4.3/keycloak-2fa-email-authenticator-v26.4.3-KC26.5.2.jar

RUN wget -q "${EMAIL_OTP_JAR_URL}" \
      -O /keycloak-2fa-email-authenticator.jar

# ── Stage 2: Keycloak ─────────────────────────────────────────────────────────
FROM quay.io/keycloak/keycloak:26.1.4

# Copy the pre-downloaded provider JAR from stage 1.
COPY --from=downloader /keycloak-2fa-email-authenticator.jar \
     /opt/keycloak/providers/keycloak-2fa-email-authenticator.jar

# Copy realm import files — loaded automatically via --import-realm on first boot.
COPY import/ /opt/keycloak/data/import/

# Bake providers into the optimised server binary (no env values read here).
USER keycloak
RUN /opt/keycloak/bin/kc.sh build

# Entrypoint inherited from base image.
# docker-compose passes: command: start --import-realm
