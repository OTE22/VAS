FROM python:3.11-slim
RUN pip install --no-cache-dir 'jupyterlab>=4.3,<5' 'ipykernel>=6.29,<7' 'pyarrow>=18,<24' \
    && useradd --create-home --uid 1000 notebook \
    && mkdir /workspace && chown notebook:notebook /workspace
COPY notebook_config.py /etc/jupyter/jupyter_server_config.py
USER 1000:1000
WORKDIR /workspace
ENV JUPYTER_CONFIG_DIR=/etc/jupyter PYTHONDONTWRITEBYTECODE=1
CMD ["jupyter", "lab", "--config=/etc/jupyter/jupyter_server_config.py"]
