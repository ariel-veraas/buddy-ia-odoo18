# Local demo provider is separate from the Odoo service.
FROM python:3.12-slim AS stub-llm
WORKDIR /app
COPY demo/requirements.txt demo/requirements.lock /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt -c /tmp/requirements.lock
COPY demo/stub_llm_server.py /app/stub_llm_server.py
USER 65534:65534
CMD ["uvicorn", "stub_llm_server:app", "--host", "0.0.0.0", "--port", "9100", "--log-level", "warning"]

FROM odoo:18.0 AS buddy
USER root
COPY odoo-addons/requirements.txt odoo-addons/requirements.lock /tmp/
# --ignore-installed: Debian ships some Python packages without RECORD files, so pip cannot uninstall them.
RUN pip3 install --no-cache-dir --break-system-packages --ignore-installed \
    -r /tmp/requirements.txt -c /tmp/requirements.lock
# The addon is self-contained: no third-party Odoo modules and no network access at build time beyond pip.
COPY odoo-addons/buddy_ia /opt/buddy/addons/buddy_ia
RUN chown -R odoo:odoo /opt/buddy/addons
USER odoo
