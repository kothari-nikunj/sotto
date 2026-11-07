ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
USER root
COPY smoke/fixed-cross/ /fixture/
RUN chown -R root:root /fixture && chmod -R go-w /fixture
