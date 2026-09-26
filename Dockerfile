FROM python:3.12-alpine
WORKDIR /app
COPY . .
ENV WEB_HOST=0.0.0.0 WEB_PORT=8000
EXPOSE 8000
CMD ["python", "web_server.py"]
