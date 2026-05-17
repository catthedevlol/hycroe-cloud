FROM python:3.12-slim

# Basic tools
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    ca-certificates \
    iproute2 \
    iputils-ping \
    dnsutils

# Install Incus CLI
RUN curl -fsSL https://pkgs.zabbly.com/key.asc | gpg --dearmor -o /usr/share/keyrings/zabbly.gpg

RUN echo "deb [signed-by=/usr/share/keyrings/zabbly.gpg] https://pkgs.zabbly.com/incus/stable $(. /etc/os-release; echo $VERSION_CODENAME) main" \
    > /etc/apt/sources.list.d/incus.list

RUN apt-get update && apt-get install -y incus

# App setup
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m -u 1000 panel && chown -R panel:panel /app

USER panel

CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]