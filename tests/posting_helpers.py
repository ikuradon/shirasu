"""Shared expectations for signed post metadata and formatter suffixes."""

from shirasu.reports.publication import ATTRIBUTION, message_hashtags, report_hashtags


def hashtag_tags(event):
    return [['t', value] for value in message_hashtags(event.content())]


def image_tags(event):
    return [tag.to_vec() for tag in event.tags() if tag.to_vec()[0] == 'imeta']


def post_footer(report):
    return ' '.join('#' + value for value in report_hashtags(report)) + '\n\n' + ATTRIBUTION
