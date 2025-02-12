"""
Backing document classes for :class:`fiftyone.core.frame.Frame` instances.

| Copyright 2017-2021, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
from collections import OrderedDict

from fiftyone.core.fields import FrameNumberField, ObjectIdField

from .document import Document, SampleDocument
from .mixins import DatasetMixin, get_default_fields, NoDatasetMixin


class DatasetFrameSampleDocument(DatasetMixin, Document, SampleDocument):

    meta = {"abstract": True}

    _sample_id = ObjectIdField(required=True)
    frame_number = FrameNumberField(required=True)

    @classmethod
    def _sample_collection_name(cls):
        return ".".join(cls.__name__.split(".")[1:])

    @classmethod
    def _dataset_doc_fields_col(cls):
        return "frame_fields"


class NoDatasetFrameSampleDocument(NoDatasetMixin, SampleDocument):

    # pylint: disable=no-member
    default_fields = DatasetFrameSampleDocument._fields
    default_fields_ordered = get_default_fields(
        DatasetFrameSampleDocument, include_private=True
    )

    def __init__(self, **kwargs):
        self._data = OrderedDict(
            [("_sample_id", None), ("frame_number", None)]
        )
        self._data.update(kwargs)
