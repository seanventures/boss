# Guest image for the BOSS sandbox.
#   • today: run as a Docker container (the stand-in) — `docker run`
#   • later: the SAME image becomes the microVM rootfs under Kata/Firecracker
#
# Based on the official Maven + Temurin JDK 17 image (toolchain built in), plus
# Python (the in-guest agent) and git. Add Node/Go/.NET + mise/sdkman here as
# more language coverage is required. The native coding agent (engine=openai) is
# the in-guest agent itself, talking to the model API over HTTPS with a per-task
# key — no CLI / keychain needed, so it works headless in a sandbox.
FROM maven:3.9-eclipse-temurin-17

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY in_guest_agent.py /opt/boss/in_guest_agent.py

ENV BOSS_SPEC_PATH=/work/spec.json \
    BOSS_RESULT_PATH=/work/result.json \
    BOSS_WORKDIR=/work/repo
WORKDIR /work
ENTRYPOINT ["python3", "/opt/boss/in_guest_agent.py"]
