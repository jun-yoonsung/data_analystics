# 백엔드 공용 이미지 (마이그레이션·설정 동기화, 이후 API/워커도 같은 이미지 사용)
# 빌드 컨텍스트는 저장소 루트 (config/ 를 함께 복사하기 위해)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/app/config

WORKDIR /app/backend
COPY backend/pyproject.toml ./
COPY backend/ ./
RUN pip install --no-cache-dir .
COPY config/ /app/config/
