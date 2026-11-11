FROM python:3.11-slim

# Установка базовых компиляторов, утилит и sudo
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    pkg-config \
    bash \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Установка Node.js 20 LTS
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Разрешаем беспарольный sudo для всех пользователей (если модели понадобится apt-get)
RUN echo "ALL ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

# Настройка Git: игнорировать несовпадение владельца файлов (защита от dubious ownership)
RUN git config --system --add safe.directory "*"

# Подготовка общей домашней директории с полными правами (для кэшей npm, pip при динамическом UID)
RUN mkdir -p /home/sandbox && chmod -R 777 /home/sandbox /tmp

# Окружение: направляем кэши в доступные директории
ENV HOME=/home/sandbox
ENV npm_config_cache=/tmp/.npm
ENV PIP_CACHE_DIR=/tmp/.pip

WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
