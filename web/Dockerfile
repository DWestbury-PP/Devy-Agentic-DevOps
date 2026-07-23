# Web `ask` surface — static terminal-themed UI served by nginx, which also
# reverse-proxies the API to the proxy service (see nginx.conf).
#
# Render libraries are fetched at BUILD time (pinned) into the image, so the
# running container makes no external calls. No Node toolchain, no build step.

FROM alpine:3.20 AS vendor
RUN apk add --no-cache curl
WORKDIR /vendor
# Pinned versions — bump deliberately.
RUN set -eux; \
    curl -fsSL -o marked.min.js     https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js; \
    curl -fsSL -o purify.min.js     https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js; \
    curl -fsSL -o highlight.min.js  https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/highlight.min.js; \
    curl -fsSL -o hljs-theme.css    https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/styles/base16/green-screen.min.css; \
    curl -fsSL -o mermaid.min.js    https://cdn.jsdelivr.net/npm/mermaid@10.9.0/dist/mermaid.min.js

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html app.js styles.css admin.html admin.js /usr/share/nginx/html/
COPY favicon.svg favicon.ico favicon-32.png apple-touch-icon.png /usr/share/nginx/html/
COPY errors /usr/share/nginx/html/errors
COPY --from=vendor /vendor /usr/share/nginx/html/vendor
EXPOSE 8080
