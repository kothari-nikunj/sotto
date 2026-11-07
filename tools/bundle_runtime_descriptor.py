#!/usr/bin/env python3
"""Validate and copy a qualified descriptor into a Bridge bundle before codesigning."""
import argparse
import re

from descriptor_bundle import bundle_descriptor, read_descriptor


def validate(raw):
    value = read_descriptor(raw, label='runtime descriptor')
    if (not isinstance(value, dict)
            or set(value) != {'version', 'image', 'sourceCommit', 'chatModelId', 'nativeMediaModelId'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['image'], str)
            or not re.fullmatch(r'ghcr\.io/kothari-nikunj/sotto-selfhost@sha256:[a-f0-9]{64}', value['image'])
            or not isinstance(value['sourceCommit'], str)
            or not re.fullmatch(r'[a-f0-9]{40}', value['sourceCommit'])
            or any(not isinstance(value[key], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value[key])
                   for key in ('chatModelId', 'nativeMediaModelId'))):
        raise ValueError('invalid runtime descriptor')
    return value


def bundle(source, destination):
    bundle_descriptor(source, destination, validate, label='runtime descriptor',
                      conflict_message='bundle already contains a different runtime release')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('destination')
    args = parser.parse_args()
    bundle(args.source, args.destination)
