include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-e5-esim
PKG_VERSION:=1.0.0
PKG_RELEASE:=6
PKG_LICENSE:=MIT
PKGARCH:=all

include $(INCLUDE_DIR)/package.mk

define Package/luci-app-e5-esim
  SECTION:=luci
  CATEGORY:=LuCI
  TITLE:=eSIM profile manager (lpac)
  DEPENDS:=+luci-base +rpcd +python3 +lpac +libcurl-gnutls4
endef

define Build/Compile
endef

define Package/luci-app-e5-esim/install
	$(INSTALL_DIR) $(1)/www
	$(CP) ./root/. $(1)/
	$(CP) ./htdocs/. $(1)/www/
	chmod 0755 $(1)/usr/libexec/rpcd/e5-esim
endef

define Package/luci-app-e5-esim/postinst
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] || {
	rm -f /tmp/luci-indexcache
	rm -rf /tmp/luci-modulecache
	/etc/init.d/rpcd restart
}
exit 0
endef

$(eval $(call BuildPackage,luci-app-e5-esim))
