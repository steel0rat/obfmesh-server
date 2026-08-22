# obfmesh server: packaging and local checks.
#
#   make deb        -> dist/obfmesh-server_<version>_all.deb
#   make wheels     -> vendor/wheels, which turns the .deb into an offline install
#   make test       -> pytest in a throwaway virtualenv
#   make lint       -> shell syntax check of the maintainer scripts
#   make clean      -> build artefacts; make distclean also drops vendor/wheels
#
# The .deb is built with dpkg-deb alone: no debhelper, no build dependencies beyond
# dpkg-deb, so it can be produced on the target box from a plain checkout. install.sh
# stays the supported way to install without a package manager; the two never run
# together (see the shadowing warning in packaging/deb/postinst).
#
# The version comes from obfmesh/__init__.py - one source of truth for the package,
# for GET /api/status and for the reports.

PKG_NAME     := obfmesh-server
VERSION      := $(shell sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' obfmesh/__init__.py)
DEB_REVISION ?= 1
DEB_VERSION  := $(VERSION)-$(DEB_REVISION)
DEB_ARCH     := all
DEB          := dist/$(PKG_NAME)_$(DEB_VERSION)_$(DEB_ARCH).deb

STAGE   := build/deb
PREFIX  := /opt/obfmesh
PYTHON  ?= python3
DEBDIR  := packaging/deb

# `pip download` for a machine that is not this one. The defaults match Ubuntu 24.04
# (CPython 3.12, x86_64); override them for another target, e.g.
#   make wheels WHEEL_MACHINE=aarch64
#   make wheels WHEEL_PYVERSION=3.11 WHEEL_ABI=cp311
#
# Several platform tags on purpose: pip matches --platform against the wheel tag
# literally, and the pinned uvloop/httptools releases publish manylinux_2_17 wheels
# only, so asking for manylinux_2_28 alone finds nothing in the required version range.
WHEEL_DIR       := vendor/wheels
WHEEL_MACHINE   ?= x86_64
WHEEL_PLATFORMS ?= manylinux_2_28_$(WHEEL_MACHINE) \
                   manylinux_2_17_$(WHEEL_MACHINE) \
                   manylinux2014_$(WHEEL_MACHINE)
WHEEL_PYVERSION ?= 3.12
WHEEL_ABI       ?= cp312

.PHONY: help version deb wheels test lint clean distclean check-version check-dpkg

help:
	@echo "obfmesh-server $(VERSION)"
	@echo ""
	@echo "  make deb        build $(DEB)"
	@echo "  make wheels     vendor the python dependencies into $(WHEEL_DIR)"
	@echo "  make test       run the test suite in a throwaway virtualenv"
	@echo "  make lint       syntax-check the deb maintainer scripts and install.sh"
	@echo "  make clean      remove build/ and dist/"
	@echo "  make distclean  also remove $(WHEEL_DIR)"
	@echo ""
	@echo "  DEB_REVISION=$(DEB_REVISION)  -> package version $(DEB_VERSION)"

version:
	@echo "$(VERSION)"

check-version:
	@[ -n "$(VERSION)" ] || { \
		echo "cannot read __version__ from obfmesh/__init__.py" >&2; exit 1; }

check-dpkg:
	@command -v dpkg-deb >/dev/null 2>&1 || { \
		echo "dpkg-deb not found: build the .deb on a Debian/Ubuntu host" >&2; \
		echo "(apt-get install dpkg-dev), or inside a container of one." >&2; exit 1; }

# ---------------------------------------------------------------- the package

# The layout mirrors what obfmesh-server.service expects: WorkingDirectory=/opt/obfmesh,
# ExecStart=/opt/obfmesh/.venv/bin/python, OBFMESH_STATIC_DIR=/opt/obfmesh/obfmesh/static.
# Moving the code elsewhere means editing the unit as well, so it stays under /opt.
# Comment lines of control.in are stripped on the way: DEBIAN/control has to be plain
# deb822, while the template carries the reasoning for its fields.
deb: check-version check-dpkg
	rm -rf $(STAGE)
	install -d -m 0755 $(STAGE)/DEBIAN
	install -d -m 0755 $(STAGE)$(PREFIX)/obfmesh/static
	install -m 0644 obfmesh/*.py $(STAGE)$(PREFIX)/obfmesh/
	install -m 0644 obfmesh/static/* $(STAGE)$(PREFIX)/obfmesh/static/
	install -m 0644 requirements.txt $(STAGE)$(PREFIX)/requirements.txt
	install -d -m 0755 $(STAGE)/usr/lib/systemd/system
	install -m 0644 obfmesh-server.service $(STAGE)/usr/lib/systemd/system/obfmesh-server.service
	install -d -m 0755 $(STAGE)/etc/logrotate.d
	install -m 0644 logrotate.obfmesh $(STAGE)/etc/logrotate.d/obfmesh
	install -d -m 0755 $(STAGE)/usr/sbin
	install -m 0755 $(DEBDIR)/obfmesh-ctl $(STAGE)/usr/sbin/obfmesh-ctl
	@if [ -d $(WHEEL_DIR) ] && [ -n "$$(ls -A $(WHEEL_DIR) 2>/dev/null)" ]; then \
		echo "vendoring $$(ls $(WHEEL_DIR) | wc -l) wheels into the package"; \
		install -d -m 0755 $(STAGE)$(PREFIX)/wheels; \
		install -m 0644 $(WHEEL_DIR)/* $(STAGE)$(PREFIX)/wheels/; \
	else \
		echo "no $(WHEEL_DIR): the postinst will need network access for pip"; \
	fi
	install -m 0644 $(DEBDIR)/conffiles $(STAGE)/DEBIAN/conffiles
	install -m 0755 $(DEBDIR)/postinst $(DEBDIR)/prerm $(DEBDIR)/postrm $(STAGE)/DEBIAN/
	sed -e '/^#/d' \
	    -e 's/@VERSION@/$(DEB_VERSION)/' \
	    -e 's/@ARCH@/$(DEB_ARCH)/' \
	    -e "s/@INSTALLED_SIZE@/$$(du -ks $(STAGE)/etc $(STAGE)/opt $(STAGE)/usr | awk '{s+=$$1} END {print s}')/" \
	    $(DEBDIR)/control.in >$(STAGE)/DEBIAN/control
	chmod 0644 $(STAGE)/DEBIAN/control
	install -d -m 0755 dist
	dpkg-deb --root-owner-group --build $(STAGE) $(DEB)
	@echo ""
	@dpkg-deb --info $(DEB) | sed -n '1,12p'
	@echo "built $(DEB)"

# ---------------------------------------------------------------- offline wheels

# Only binary wheels: a source distribution would have to be compiled on the target,
# which is exactly what an offline install cannot do. uvloop and httptools ship
# manylinux wheels, so the whole requirements.txt resolves.
wheels:
	rm -rf $(WHEEL_DIR)
	install -d -m 0755 $(WHEEL_DIR)
	$(PYTHON) -m pip download --requirement requirements.txt --dest $(WHEEL_DIR) \
		--only-binary=:all: --implementation cp \
		$(foreach p,$(WHEEL_PLATFORMS),--platform $(p)) \
		--python-version $(WHEEL_PYVERSION) \
		--abi $(WHEEL_ABI)
	@echo "wheels for $(WHEEL_MACHINE)/$(WHEEL_ABI) in $(WHEEL_DIR); 'make deb' picks them up"

# ---------------------------------------------------------------- checks

# The suite is dry-run only: no root, no Linux, no WireGuard needed.
test:
	$(PYTHON) -m venv build/venv-test
	build/venv-test/bin/python -m pip install --quiet --upgrade pip
	build/venv-test/bin/python -m pip install --quiet --requirement requirements.txt \
		--requirement requirements-dev.txt
	build/venv-test/bin/python -m pytest tests -q

lint:
	sh -n $(DEBDIR)/postinst
	sh -n $(DEBDIR)/prerm
	sh -n $(DEBDIR)/postrm
	sh -n $(DEBDIR)/obfmesh-ctl
	bash -n install.sh
	@echo "shell syntax ok"

clean:
	rm -rf build dist

distclean: clean
	rm -rf $(WHEEL_DIR)
