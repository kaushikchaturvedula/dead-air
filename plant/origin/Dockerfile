FROM nginx:1.27-alpine

# Real log directory. The image's /var/log/nginx/access.log is a symlink to
# /dev/stdout, which cannot be tailed by a log shipper -- see nginx.conf.
RUN mkdir -p /var/log/deadair

COPY nginx.conf /etc/nginx/nginx.conf
COPY player/ /usr/share/nginx/player/
EXPOSE 8080
