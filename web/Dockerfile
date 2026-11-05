FROM nginx:1.28-alpine
COPY web/default.conf.template /etc/nginx/templates/default.conf.template
COPY --chmod=755 web/15-resolver.envsh /docker-entrypoint.d/15-resolver.envsh
COPY web/public/ /usr/share/nginx/html/
ENV PORT=8080 API_UPSTREAM=api:8000 IDENTITY_UPSTREAM=api:8000
EXPOSE 8080
