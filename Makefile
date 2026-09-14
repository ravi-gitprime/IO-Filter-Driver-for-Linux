# SPDX-License-Identifier: GPL-2.0
KVER ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KVER)/build
CC   ?= gcc
CFLAGS_TOOLS := -O2 -Wall -Wextra -I$(CURDIR)/include/uapi

TOOLS := tools/cdp-drain tools/cdp-apply

all: modules tools

modules:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

tools: $(TOOLS)

tools/%: tools/%.c include/uapi/linux/dm-cdp.h
	$(CC) $(CFLAGS_TOOLS) -o $@ $<

install: modules
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules_install
	depmod -a

load: modules
	-rmmod dm-cdp 2>/dev/null
	insmod dm-cdp.ko

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
	rm -f $(TOOLS)

checkpatch:
	$(KDIR)/scripts/checkpatch.pl --no-tree -f drivers/md/dm-cdp.c include/uapi/linux/dm-cdp.h

.PHONY: all modules tools install load clean checkpatch
