PREFIX ?= /usr/local
UNITDIR ?= /etc/systemd/system

install:
	install -Dm755 console-mouse $(DESTDIR)$(PREFIX)/bin/console-mouse
	install -Dm644 console-mouse.service $(DESTDIR)$(UNITDIR)/console-mouse.service
	systemctl daemon-reload
	systemctl enable console-mouse.service
	systemctl restart console-mouse.service

uninstall:
	systemctl disable --now console-mouse.service
	rm -f $(DESTDIR)$(PREFIX)/bin/console-mouse $(DESTDIR)$(UNITDIR)/console-mouse.service
	systemctl daemon-reload

test:
	python3 tests/vt-test.py

.PHONY: install uninstall test
