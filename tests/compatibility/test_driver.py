"""Tests Let's Encrypt plugins against different server configurations."""
import argparse
import filecmp
import functools
import logging
import os
import shutil
import tempfile

import OpenSSL

from acme import challenges
from acme import crypto_util
from acme import messages
from letsencrypt import achallenges
from letsencrypt import errors as le_errors
from letsencrypt import validator
from letsencrypt.tests import acme_util
from tests.compatibility import errors
from tests.compatibility import util
from tests.compatibility.configurators.apache import apache24


DESCRIPTION = """
Tests Let's Encrypt plugins against different server configuratons. It is
assumed that Docker is already installed. If no test types is specified, all
tests that the plugin supports are performed.

"""

PLUGINS = {"apache" : apache24.Proxy}


logger = logging.getLogger(__name__)


def test_authenticator(plugin, config, temp_dir):
    """Tests authenticator, returning True if the tests are successful"""
    backup = _create_backup(config, temp_dir)

    achalls = _create_achalls(plugin)
    if not achalls:
        # Plugin/tests support no common challenge types
        return True

    try:
        responses = plugin.perform(achalls)
    except le_errors.Error as error:
        logger.error("Performing challenges on %s caused an error:", config)
        logger.exception(error)
        return False

    success = True
    for i in xrange(len(responses)):
        if not responses[i]:
            logger.error(
                "Plugin failed to complete %s for %s in %s",
                type(achalls[i]), achalls[i].domain, config)
            success = False
        elif isinstance(responses[i], challenges.DVSNIResponse):
            if responses[i].simple_verify(achalls[i],
                                          achalls[i].domain,
                                          util.JWK.key.public_key(),
                                          host="127.0.0.1",
                                          port=plugin.https_port):
                logger.info(
                    "DVSNI verification for %s succeeded", achalls[i].domain)
            else:
                logger.error(
                    "DVSNI verification for %s in %s failed",
                    achalls[i].domain, config)
                success = False

    if success:
        try:
            plugin.cleanup(achalls)
        except le_errors.Error as error:
            logger.error("Challenge cleanup for %s caused an error:", config)
            logger.exception(error)
            success = False

        if _dirs_are_unequal(config, backup):
            logger.error("Challenge cleanup failed for %s", config)
            return False
        else:
            logger.info("Challenge cleanup succeeded")

    return success


def _create_achalls(plugin):
    """Returns a list of annotated challenges to test on plugin"""
    achalls = list()
    names = plugin.get_testable_domain_names()
    for domain in names:
        prefs = plugin.get_chall_pref(domain)
        for chall_type in prefs:
            if chall_type == challenges.DVSNI:
                chall = challenges.DVSNI(
                    r=os.urandom(challenges.DVSNI.R_SIZE),
                    nonce=os.urandom(challenges.DVSNI.NONCE_SIZE))
                challb = acme_util.chall_to_challb(
                    chall, messages.STATUS_PENDING)
                achall = achallenges.DVSNI(
                    challb=challb, domain=domain, key=util.JWK)
                achalls.append(achall)

    return achalls


def test_installer(args, plugin, config, temp_dir):
    """Tests plugin as an installer"""
    backup = _create_backup(config, temp_dir)

    names_match = plugin.get_all_names() == plugin.get_all_names_answer()
    if names_match:
        logger.info("get_all_names test succeeded")
    else:
        logger.error("get_all_names test failed for config %s", config)

    domains = list(plugin.get_testable_domain_names())
    success = test_deploy_cert(plugin, temp_dir, domains)

    if success and args.enhance:
        success = test_enhancements(plugin, domains)

    good_rollback = test_rollback(plugin, config, backup)
    return names_match and success and good_rollback


def test_deploy_cert(plugin, temp_dir, domains):
    """Tests deploy_cert returning True if the tests are successful"""
    cert = crypto_util.gen_ss_cert(util.KEY, domains)
    cert_path = os.path.join(temp_dir, "cert.pem")
    with open(cert_path, "w") as f:
        f.write(OpenSSL.crypto.dump_certificate(
            OpenSSL.crypto.FILETYPE_PEM, cert))

    for domain in domains:
        try:
            plugin.deploy_cert(domain, cert_path, util.KEY_PATH)
        except le_errors.Error as error:
            logger.error("Plugin failed to deploy ceritificate for %s:", domain)
            logger.exception(error)
            return False

    if not _save_and_restart(plugin, "deployed"):
        return False

    verify_cert = validator.Validator().certificate
    success = True
    for domain in domains:
        if not verify_cert(cert, domain, "127.0.0.1", plugin.https_port):
            logger.error("Could not verify certificate for domain %s", domain)
            success = False

    if success:
        logger.info("HTTPS validation succeeded")

    return success


def test_enhancements(plugin, domains):
    """Tests supported enhancements returning True if successful"""
    supported = plugin.supported_enhancements()

    if "redirect" not in supported:
        return True

    for domain in domains:
        try:
            plugin.enhance(domain, "redirect")
        except le_errors.Error as error:
            logger.error("Plugin failed to enable redirect for %s:", domain)
            logger.exception(error)
            return False

    if not _save_and_restart(plugin, "enhanced"):
        return False

    verify_redirect = functools.partial(
        validator.Validator().redirect, "localhost", plugin.http_port)
    success = True
    for domain in domains:
        if not verify_redirect(headers={"Host" : domain}):
            logger.error("Improper redirect for domain %s", domain)
            success = False

    if success:
        logger.info("Enhancments test succeeded")

    return success


def _save_and_restart(plugin, title=None):
    """Saves and restart the plugin, returning True if no errors occurred"""
    try:
        plugin.save(title)
        plugin.restart()
        return True
    except le_errors.Error as error:
        logger.error("Plugin failed to save and restart server:")
        logger.exception(error)
        return False


def test_rollback(plugin, config, backup):
    """Tests the rollback checkpoints function"""
    try:
        plugin.rollback_checkpoints(2)
    except le_errors.Error as error:
        logger.error("Plugin raised an exception during rollback:")
        logger.exception(error)
        return False

    if _dirs_are_unequal(config, backup):
        logger.error("Rollback failed for config `%s`", config)
        return False
    else:
        logger.info("Rollback succeeded")
        return True


def _create_backup(config, temp_dir):
    """Creates a backup of config in temp_dir"""
    backup = os.path.join(temp_dir, "backup")
    shutil.rmtree(backup, ignore_errors=True)
    shutil.copytree(config, backup, symlinks=True)

    return backup


def _dirs_are_unequal(dir1, dir2):
    """Returns True if dir1 and dir2 are equal"""
    dircmp = filecmp.dircmp(dir1, dir2)

    return (dircmp.left_only or dircmp.right_only or
            dircmp.diff_files or dircmp.funny_files)


def get_args():
    """Returns parsed command line arguments."""
    parser = argparse.ArgumentParser(
        description=DESCRIPTION,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    group = parser.add_argument_group("general")
    group.add_argument(
        "-c", "--configs", default="configs.tar.gz",
        help="a directory or tarball containing server configurations")
    group.add_argument(
        "-p", "--plugin", default="apache", help="the plugin to be tested")
    group.add_argument(
        "-v", "--verbose", dest="verbose_count", action="count",
        default=0, help="you know how to use this")
    group.add_argument(
        "-a", "--auth", action="store_true",
        help="tests the challenges the plugin supports")
    group.add_argument(
        "-i", "--install", action="store_true",
        help="tests the plugin as an installer")
    group.add_argument(
        "-e", "--enhance", action="store_true", help="tests the enhancements "
        "the plugin supports (implicitly includes installer tests)")

    for plugin in PLUGINS.itervalues():
        plugin.add_parser_arguments(parser)

    args = parser.parse_args()
    if args.enhance:
        args.install = True
    elif not (args.auth or args.install):
        args.auth = args.install = args.enhance = True

    return args


def setup_logging(args):
    """Prepares logging for the program"""
    handler = logging.StreamHandler()

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.WARNING - args.verbose_count * 10)
    root_logger.addHandler(handler)


def main():
    """Main test script execution."""
    args = get_args()
    setup_logging(args)

    if args.plugin not in PLUGINS:
        raise errors.Error("Unknown plugin {0}".format(args.plugin))

    temp_dir = tempfile.mkdtemp()
    plugin = PLUGINS[args.plugin](args)
    try:
        plugin.execute_in_docker("mkdir -p /var/log/apache2")
        while plugin.has_more_configs():
            success = True

            try:
                config = plugin.load_config()
                logger.info("Loaded configuration: %s", config)
                if args.auth:
                    success = test_authenticator(plugin, config, temp_dir)
                if success and args.install:
                    success = test_installer(args, plugin, config, temp_dir)
            except errors.Error as error:
                logger.error("Tests on %s raised:", config)
                logger.exception(error)
                success = False

            if success:
                logger.info("All tests on %s succeeded", config)
            else:
                logger.error("Tests on %s failed", config)
    finally:
        plugin.cleanup_from_tests()


if __name__ == "__main__":
    main()
