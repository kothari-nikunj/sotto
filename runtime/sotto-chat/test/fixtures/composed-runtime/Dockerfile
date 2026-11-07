# Test-only composition layer. Build only after Dockerfile.runtime created BASE_IMAGE.
ARG BASE_IMAGE=sotto-chat-runtime:composed-base
FROM ${BASE_IMAGE}
USER root
RUN useradd --non-unique --uid 10001 --gid 10001 --home-dir /home/sotto \
      --create-home --shell /usr/sbin/nologin sotto
COPY runtime/sotto-chat/test/fixtures/composed-runtime/requirements.txt /tmp/composed-runtime-requirements.txt
RUN python3 -m pip install --no-cache-dir --require-hashes \
      -r /tmp/composed-runtime-requirements.txt \
 && rm /tmp/composed-runtime-requirements.txt
COPY adapters/sotto/ /app/adapters/sotto/
COPY runtime/trigger-receiver/ /app/trigger-receiver/
COPY sotto-chief-of-staff/_shared/lib/tzchain.py /app/trigger-receiver/tzchain.py
COPY sotto-chief-of-staff/_shared/lib/history_coverage.py /app/trigger-receiver/history_coverage.py
COPY sotto-chief-of-staff/_shared/lib/work_queue.py /app/trigger-receiver/work_queue.py
COPY sotto-chief-of-staff/_shared/lib/routine_cursor.py /app/trigger-receiver/routine_cursor.py
COPY sotto-chief-of-staff/_shared/lib/routine_cursor.NOTICE /app/trigger-receiver/routine_cursor.NOTICE
COPY sotto-chief-of-staff/_shared/lib/calendar_context.py /app/trigger-receiver/calendar_context.py
COPY sotto-chief-of-staff/_shared/lib/source_catalog.py /app/trigger-receiver/source_catalog.py
COPY sotto-chief-of-staff/_shared/lib/message_targets.py /app/trigger-receiver/message_targets.py
COPY runtime/sotto-chat/test/fixtures/composed-runtime/ /app/runtime/sotto-chat/test/fixtures/composed-runtime/
