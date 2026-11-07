#!/usr/bin/env python3
"""Seal registered public/native Railway client metadata into Bridge before signing."""
import argparse
import re

from descriptor_bundle import bundle_descriptor, read_descriptor


def validate(raw):
    value = read_descriptor(raw, label='Railway OAuth descriptor')
    if (not isinstance(value, dict) or set(value) != {'version', 'clientID', 'redirectURI'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['clientID'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', value['clientID'])
            or value['redirectURI'] != 'ai.sotto.bridge://railway/oauth'):
        raise ValueError('invalid Railway OAuth descriptor')
    return value


def bundle(source, destination):
    bundle_descriptor(source, destination, validate, label='Railway OAuth descriptor',
                      conflict_message='bundle already contains a different Railway OAuth client')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('destination')
    args = parser.parse_args()
    bundle(args.source, args.destination)
