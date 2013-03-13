#
# Copyright (c) 2010 Red Hat, Inc.
#
# Authors: Jeff Ortel <jortel@redhat.com>
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.
#

import os
import logging
import simplejson as json
import gettext
import yum
_ = gettext.gettext

from gzip import GzipFile
from rhsm.certificate import create_from_pem
from subscription_manager.certdirectory import Directory, ProductDirectory

from subscription_manager import plugins

log = logging.getLogger('rhsm-app.' + __name__)


class DatabaseDirectory(Directory):

    PATH = 'var/lib/rhsm'

    def __init__(self):
        Directory.__init__(self, self.PATH)
        self.create()


class ProductDatabase:

    def __init__(self):
        self.dir = DatabaseDirectory()
        self.content = {}
        self.create()

    def add(self, product, repo):
        self.content[product] = repo

    def delete(self, product):
        try:
            del self.content[product]
        except:
            pass

    def find_repo(self, product):
        return self.content.get(product)

    def create(self):
        if not os.path.exists(self.__fn()):
            self.write()

    def read(self):
        f = open(self.__fn())
        try:
            d = json.load(f)
            self.content = d
        except:
            pass
        f.close()

    def write(self):
        f = open(self.__fn(), 'w')
        try:
            json.dump(self.content, f, indent=2)
        except:
            pass
        f.close()

    def __fn(self):
        return self.dir.abspath('productid.js')


class ProductManager:

    REPO = 'from_repo'
    PRODUCTID = 'productid'

    def __init__(self, product_dir=None, product_db=None):

        self.pdir = product_dir
        if not product_dir:
            self.pdir = ProductDirectory()

        self.db = product_db
        if not product_db:
            self.db = ProductDatabase()

        self.db.read()
        self.meta_data_errors = []

        self.plugin_manager = plugins.getPluginManager()

    def update(self, yb):
        if yb is None:
            yb = yum.YumBase()
        enabled = self.get_enabled(yb)
        active = self.get_active(yb)

        # only execute this on versions of yum that track
        # which repo a package came from, aka, 3.2.28 and newer
        if self._check_yum_version_tracks_repos():
            # check that we have any repo's enabled
            # and that we have some enabled repo's. Not just
            # that we have packages from repo's that are
            # not active. See #806457
            if enabled and active:
                self.update_removed(active)
        # FIXME: it would probably be useful to keep track of
        # the state a bit, so we can report what we did
        self.update_installed(enabled, active)

    def _check_yum_version_tracks_repos(self):
        major, minor, micro = yum.__version_info__
        if major >= 3 and minor >= 2 and micro >= 28:
            return True
        return False

    def _is_workstation(self, product_cert):
        if product_cert.name == "Red Hat Enterprise Linux Workstation" and \
                "rhel-5-client-workstation" in product_cert.provided_tags and \
                product_cert.version[0] == '5':
            return True
        return False

    def _is_desktop(self, product_cert):
        if product_cert.name == "Red Hat Enterprise Linux Desktop" and \
                "rhel-5-client" in product_cert.provided_tags and \
                product_cert.version[0] == '5':
            return True
        return False

    def update_installed(self, enabled, active):
        log.debug("Updating installed certificates")
        products_installed = []
        for cert, repo in enabled:
            log.debug("product cert: %s repo: %s" % (cert.products[0].id, repo))
            #nothing from this repo is installed
            if repo not in active:
                continue

            # is this the same as v1 ProductCertificate.getProduct() ?
            # assume [0] indexed item is the same item
            p = cert.products[0]
            prod_hash = p.id

            # Are we installing Workstation cert?
            if self._is_workstation(p):
                # is the Desktop product cert installed?
                for pc in self.pdir.list():
                    if self._is_desktop(pc.products[0]):
                        log.info("Removing obsolete Desktop cert: %s" % pc.path)
                        # Desktop product cert is installed,
                        # delete the Desktop product cert
                        pc.delete()
                        self.pdir.refresh()  # must refresh to see the removal of the cert
                        self.db.delete(pc.products[0].id)
                        self.db.write()

            # If installing Desktop cert, see if Workstation exists on disk and skip
            # the write if so:
            if self._is_desktop(p):
                if self._workstation_cert_exists():
                    log.info("Skipping obsolete Desktop cert")
                    continue

            # Product cert already exists, no need to write:
            if self.pdir.findByProduct(prod_hash):
                continue

            fn = '%s.pem' % prod_hash
            path = self.pdir.abspath(fn)
            cert.write(path)
            self.pdir.refresh()  # must refresh product dir to see changes
            log.info("Installed product cert: %s %s" % (p.name, cert.path))
            self.db.add(prod_hash, repo)
            self.db.write()

            # return associated repo's as well?
            products_installed.append(cert)

        log.debug("about to run post_product_id_install")
        self.plugin_manager.run('post_product_id_install', product_list=products_installed)
        return products_installed

    def _workstation_cert_exists(self):
        for pc in self.pdir.list():
            if self._is_workstation(pc.products[0]):
                return True
        return False

    # We should only delete productcerts if there are no
    # packages from that repo installed (not "active")
    # and we have the product cert installed.
    def update_removed(self, active):
        for cert in self.pdir.list():
            p = cert.products[0]
            prod_hash = p.id
            repo = self.db.find_repo(prod_hash)

            # if we had errors with the repo or productid metadata
            # we could be very confused here, so do not
            # delete anything. see bz #736424
            if repo in self.meta_data_errors:
                log.info("%s has meta-data errors.  Not deleting product cert %s." % (repo, prod_hash))
                continue

            # FIXME: not entirely sure why we do this
            #  to avoid a none on cert.delete surely
            # but is there another reason?
            if repo is None:
                continue
            if repo in active:
                continue

            # TODO/FIXME: plugin call on cert delete specifically?
            log.info("product cert %s for %s is being deleted" % (prod_hash, p.name))
            cert.delete()
            self.pdir.refresh()

            self.db.delete(prod_hash)
            self.db.write()

    # find the list of repo's that provide packages that
    # are actually installed.
    def get_active(self, yb):
        """find yum repos that have packages installed"""
        active = set()

        packages = yb.pkgSack.returnPackages()
        for p in packages:
            repo = p.repoid

            # if a pkg is in multiple repo's, this will consider
            # all the repo's with the pkg "active".
            db_pkg = yb.rpmdb.searchNevra(name=p.name, arch=p.arch)

            # that pkg is not actually installed
            if not db_pkg:
                continue

            # yum on 5.7 list everything as "installed" instead
            # of the repo it came from
            if repo in (None, "installed"):
                continue
            active.add(repo)
        return active

    def get_enabled(self, yb):
        """find yum repos that are enabled"""
        lst = []
        enabled = yb.repos.listEnabled()

        for repo in enabled:
            try:
                fn = repo.retrieveMD(self.PRODUCTID)
                cert = self.__get_cert(fn)
                if cert is None:
                    continue
                lst.append((cert, repo.id))
            except Exception, e:
                log.warn("Error loading productid metadata for %s." % repo)
                log.exception(e)
                self.meta_data_errors.append(repo.id)
        return lst

    def __get_cert(self, fn):
        if fn.endswith('.gz'):
            f = GzipFile(fn)
        else:
            f = open(fn)
        try:
            pem = f.read()
            return create_from_pem(pem)
        finally:
            f.close()

if __name__ == '__main__':
    pm = ProductManager()
    pm.update(yb=None)
