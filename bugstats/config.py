# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json


__CONFIG = None

def _get_global():
    global __CONFIG
    if not __CONFIG:
        with open('./config/config.json', 'r') as In:
            __CONFIG = json.load(In)
    return __CONFIG


def get_products_blacklist():
    return set(_get_global()['products: blacklist'])


def get_components_blacklist():
    return set(_get_global()['components: blacklist'])
