"""
Video frames.

| Copyright 2017-2021, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
from pymongo import ReplaceOne, UpdateOne, DeleteOne
from pymongo.errors import BulkWriteError

from fiftyone.core.document import Document, DocumentView
import fiftyone.core.frame_utils as fofu
import fiftyone.core.odm as foo
from fiftyone.core.singletons import FrameSingleton
import fiftyone.core.utils as fou


def get_default_frame_fields(include_private=False, include_id=False):
    """Returns the default fields present on all frames.

    Args:
        include_private (False): whether to include fields that start with
            ``_``
        include_id (False): whether to include ID fields

    Returns:
        a tuple of field names
    """
    return foo.get_default_fields(
        foo.DatasetFrameSampleDocument,
        include_private=include_private,
        include_id=include_id,
    )


class Frames(object):
    """An ordered dictionary of :class:`Frame` instances keyed by frame number
    representing the frames of a video :class:`fiftyone.core.sample.Sample`.

    :class:`Frames` instances behave like ``defaultdict(Frame)`` instances; an
    empty :class:`Frame` instance is returned when accessing a new frame
    number.

    :class:`Frames` instances should never be created manually; they are
    instantiated automatically when video :class:`fiftyone.core.sample.Sample`
    instances are created.

    Args:
        sample: the :class:`fiftyone.core.sample.Sample` to which the frames
            are attached
    """

    def __init__(self, sample):
        self._sample = sample
        self._iter = None
        self._replacements = {}
        self._delete_frames = set()
        self._delete_all = False

    def __str__(self):
        return "<%s: %s>" % (self.__class__.__name__, fou.pformat(dict(self)))

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, len(self))

    def __bool__(self):
        return len(self) > 0

    def __len__(self):
        return len(self._get_frame_numbers())

    def __contains__(self, frame_number):
        return frame_number in self._get_frame_numbers()

    def __getitem__(self, frame_number):
        fofu.validate_frame_number(frame_number)

        if frame_number in self._replacements:
            return self._replacements[frame_number]

        if not self._in_db:
            frame = Frame(frame_number=frame_number)  # empty frame
            self._set_replacement(frame)
            return frame

        if self._delete_all or frame_number in self._delete_frames:
            d = None
        else:
            d = self._get_frame_db(frame_number)

        if d is None:
            # Empty frame
            d = {"_sample_id": self._sample._id, "frame_number": frame_number}

        frame = self._make_frame(d)
        self._set_replacement(frame)

        return frame

    def __setitem__(self, frame_number, frame):
        self.add_frame(frame_number, frame)

    def __delitem__(self, frame_number):
        self._replacements.pop(frame_number, None)

        if not self._in_db:
            return

        self._delete_frames.add(frame_number)

    def __iter__(self):
        self._iter = self.keys()
        return self

    def __next__(self):
        try:
            return next(self._iter)
        except StopIteration:
            self._iter = None
            raise

    @property
    def _in_db(self):
        return self._sample._in_db

    @property
    def _dataset(self):
        return self._sample._dataset

    @property
    def _frame_collection(self):
        return self._sample._dataset._frame_collection

    @property
    def _frame_collection_name(self):
        return self._sample._dataset._frame_collection_name

    @property
    def field_names(self):
        """An ordered tuple of the names of the fields on the frames."""
        return list(self._dataset.get_frame_field_schema().keys())

    def first(self):
        """Returns the first :class:`Frame` for the sample.

        Returns:
            a :class:`Frame`
        """
        try:
            return next(self.values())
        except StopIteration:
            id_str = " '%s'" % self._sample.id if self._sample.id else ""
            raise ValueError("Sample%s has no frame labels" % id_str)

    def keys(self):
        """Returns an iterator over the frame numbers with labels in the
        sample.

        The frames are traversed in ascending order.

        Returns:
            a generator that emits frame numbers
        """
        for frame_number in sorted(self._get_frame_numbers()):
            yield frame_number

    def items(self):
        """Returns an iterator over the frame numberes and :class:`Frame`
        instances for the sample.

        The frames are traversed in ascending order.

        Returns:
            a generator that emits ``(frame_number, Frame)`` tuples
        """
        for frame in self._iter_frames():
            yield frame.frame_number, frame

    def values(self):
        """Returns an iterator over the :class:`Frame` instances for the
        sample.

        The frames are traversed in ascending order.

        Returns:
            a generator that emits :class:`Frame` instances
        """
        for frame in self._iter_frames():
            yield frame

    def add_frame(self, frame_number, frame, expand_schema=True):
        """Adds the frame to this instance.

        If an existing frame with the same frame number exists, it is
        overwritten.

        If the provided frame is a :class:`Frame` instance that does not belong
        to a dataset, it is updated in-place to reflect its membership in this
        dataset. Otherwise, the provided frame is not modified.

        Args:
            frame_number: the frame number
            frame: a :class:`Frame` or :class:`FrameView`
            expand_schema (True): whether to dynamically add new frame fields
                encountered to the dataset schema. If False, an error is raised
                if the frame's schema is not a subset of the dataset schema
        """
        fofu.validate_frame_number(frame_number)

        if not isinstance(frame, (Frame, FrameView)):
            raise ValueError(
                "Expected a %s or %s; found %s"
                % (Frame, FrameView, type(frame))
            )

        if self._in_db:
            _frame = frame
            if frame._in_db:
                frame = Frame()

            d = {"_sample_id": self._sample._id}
            doc = self._dataset._frame_dict_to_doc(d)
            for field, value in _frame.iter_fields():
                doc.set_field(field, value, create=expand_schema)

            doc.set_field("frame_number", frame_number)
            frame._set_backing_doc(doc, dataset=self._dataset)
        else:
            if frame._in_db:
                frame = frame.copy()

            frame.set_field("frame_number", frame_number)

        self._set_replacement(frame)

    def update(self, frames, overwrite=True, expand_schema=True):
        """Adds the frame labels to this instance.

        Args:
            frames: can be any of the following

                -   a :class:`Frames` instance
                -   a dictionary mapping frame numbers to :class:`Frame`
                    instances
                -   a dictionary mapping frame numbers to dictionaries mapping
                    label fields to :class:`fiftyone.core.labels.Label`
                    instances

            overwrite (True): whether to overwrite existing frames
            expand_schema (True): whether to dynamically add new frame fields
                encountered to the dataset schema. If False, an error is raised
                if the frame's schema is not a subset of the dataset schema
        """
        for frame_number, frame in frames.items():
            if overwrite or frame_number not in self:
                if isinstance(frame, dict):
                    frame = Frame(frame_number=frame_number, **frame)

                self.add_frame(
                    frame_number, frame, expand_schema=expand_schema
                )

    def merge(
        self,
        frames,
        omit_fields=None,
        omit_none_fields=True,
        overwrite=True,
        expand_schema=True,
    ):
        """Merges the given frames into this instance.

        Args:
            frames: can be any of the following

                -   a :class:`Frames` instance
                -   a dictionary mapping frame numbers to :class:`Frame`
                    instances
                -   a dictionary mapping frame numbers to dictionaries mapping
                    label fields to :class:`fiftyone.core.labels.Label`
                    instances

            omit_fields (None): an optional list of fields to omit
            omit_none_fields (True): whether to omit ``None``-valued fields of
                the provided frames
            overwrite (True): whether to overwrite existing fields
            expand_schema (True): whether to dynamically add new frame fields
                encountered to the dataset schema. If False, an error is raised
                if the frame's schema is not a subset of the dataset schema
        """
        for frame_number, frame in frames.items():
            if isinstance(frame, dict):
                frame = Frame(frame_number=frame_number, **frame)

            if frame_number in self:
                self[frame_number].merge(
                    frame,
                    omit_fields=omit_fields,
                    omit_none_fields=omit_none_fields,
                    overwrite=overwrite,
                    expand_schema=expand_schema,
                )
            else:
                self.add_frame(
                    frame_number, frame, expand_schema=expand_schema
                )

    def clear(self):
        """Removes all frames from this instance."""
        self._replacements.clear()

        if not self._in_db:
            return

        self._delete_all = True
        self._delete_frames.clear()

    def save(self):
        """Saves all frames to the database."""
        if not self._in_db:
            return

        self._save_deletions()
        self._save_replacements()

    def reload(self, hard=False):
        """Reloads all frames for the sample from the database.

        Args:
            hard (False): whether to reload the frame schema in addition to the
                field values for the frames. This is necessary if new fields
                may have been added to the dataset's frame schema
        """
        self._delete_all = False
        self._delete_frames.clear()
        self._replacements.clear()

        Frame._sync_docs_for_sample(
            self._frame_collection_name,
            self._sample.id,
            self._get_frame_numbers(),
            hard=hard,
        )

    def _get_frame_numbers(self):
        frame_numbers = set(self._replacements.keys())

        if not self._in_db or self._delete_all:
            return frame_numbers

        frame_numbers |= self._get_frame_numbers_db()
        frame_numbers -= self._delete_frames

        return frame_numbers

    def _get_frame_db(self, frame_number):
        return self._frame_collection.find_one(
            {"_sample_id": self._sample._id, "frame_number": frame_number}
        )

    def _get_frame_numbers_db(self):
        pipeline = [
            {"$match": {"_sample_id": self._sample._id}},
            {
                "$group": {
                    "_id": None,
                    "frame_numbers": {"$push": "$frame_number"},
                }
            },
        ]

        try:
            d = next(foo.aggregate(self._frame_collection, pipeline))
            return set(d["frame_numbers"])
        except StopIteration:
            return set()

    def _set_replacement(self, frame):
        self._replacements[frame.frame_number] = frame

    def _iter_frames(self):
        if not self._in_db or self._delete_all:
            for frame_number in sorted(self._replacements.keys()):
                yield self._replacements[frame_number]

            return

        if self._replacements:
            max_repl_fn = max(self._replacements.keys())
            repl_done = False
        else:
            max_repl_fn = -1
            repl_done = True

        results = self._iter_frames_db()

        try:
            d = next(results)
            db_done = False
        except StopIteration:
            d = None
            db_done = True

        frame_number = 1
        while True:
            if repl_done and db_done:
                break

            if not repl_done and frame_number in self._replacements:
                yield self._replacements[frame_number]

            elif (
                not db_done
                and frame_number == d["frame_number"]
                and frame_number not in self._delete_frames
            ):
                frame = self._make_frame(d)
                self._set_replacement(frame)

                yield frame

            frame_number += 1

            if not repl_done:
                repl_done = max_repl_fn < frame_number

            if not db_done:
                while d["frame_number"] < frame_number:
                    try:
                        d = next(results)
                    except StopIteration:
                        db_done = True
                        break

    def _iter_frames_db(self):
        pipeline = [
            {"$match": {"_sample_id": self._sample._id}},
            {"$sort": {"frame_number": 1}},
        ]
        return foo.aggregate(self._frame_collection, pipeline)

    def _make_frame(self, d):
        doc = self._dataset._frame_dict_to_doc(d)
        return Frame.from_doc(doc, dataset=self._dataset)

    def _make_dict(self, frame):
        d = frame.to_mongo_dict()
        d.pop("_id", None)
        d["_sample_id"] = self._sample._id
        return d

    def _to_frames_dict(self):
        return {str(fn): frame.to_dict() for fn, frame in self.items()}

    def _save_deletions(self):
        if self._delete_all:
            self._frame_collection.delete_many(
                {"_sample_id": self._sample._id}
            )

            Frame._reset_docs(
                self._frame_collection_name, sample_ids=[self._sample.id]
            )

            self._delete_all = False
            self._delete_frames.clear()

        if self._delete_frames:
            ops = [
                DeleteOne(
                    {
                        "_sample_id": self._sample._id,
                        "frame_number": frame_number,
                    }
                )
                for frame_number in self._delete_frames
            ]
            self._frame_collection.bulk_write(ops, ordered=False)

            Frame._reset_docs_for_sample(
                self._frame_collection_name,
                self._sample.id,
                self._delete_frames,
            )

            self._delete_frames.clear()

    def _save_replacements(self, include_singletons=True):
        if include_singletons:
            #
            # Since frames are singletons, the user will expect changes to any
            # in-memory frames to be saved, even if they aren't currently in
            # `_replacements`. This can happen, if, for example, our
            # replacements were flushed by a previous call to `sample.save()`
            # but then an in-memory frame was modified without explicitly
            # accessing it via `sample.frames[]`
            #
            replacements = Frame._get_instances(
                self._frame_collection_name, self._sample.id
            )
        else:
            replacements = None

        if replacements:
            replacements.update(self._replacements)
        else:
            replacements = self._replacements

        if not replacements:
            return

        #
        # Insert new frames
        #

        new_frames = [
            frame for frame in replacements.values() if not frame._in_db
        ]

        if new_frames:
            dicts = [self._make_dict(frame) for frame in new_frames]

            try:
                # adds `_id` to each dict
                self._frame_collection.insert_many(dicts)
            except BulkWriteError as bwe:
                msg = bwe.details["writeErrors"][0]["errmsg"]
                raise ValueError(msg) from bwe

            for frame, d in zip(new_frames, dicts):
                if isinstance(frame._doc, foo.NoDatasetFrameSampleDocument):
                    doc = self._dataset._frame_dict_to_doc(d)
                    frame._set_backing_doc(doc, dataset=self._dataset)
                else:
                    frame._doc.id = d["_id"]

            for frame in new_frames:
                replacements.pop(frame.frame_number, None)

        #
        # Replace existing frames
        #

        if replacements:
            ops = []
            for frame_number, frame in replacements.items():
                ops.append(
                    ReplaceOne(
                        {
                            "frame_number": frame_number,
                            "_sample_id": self._sample._id,
                        },
                        self._make_dict(frame),
                        upsert=True,
                    )
                )

            self._frame_collection.bulk_write(ops, ordered=False)

        self._replacements.clear()


class FramesView(Frames):
    """An ordered dictionary of :class:`FrameView` instances keyed by frame
    number representing the frames of a video
    :class:`fiftyone.core.sample.SampleView`.

    :class:`FramesView` instances behave like ``defaultdict(FrameView)``
    instances; an empty :class:`FrameView` instance is returned when accessing
    a new frame number.

    :class:`FramesView` instances should never be created manually; they are
    instantiated automatically when video
    :class:`fiftyone.core.sample.SampleView` instances are created.

    Args:
        sample_view: the :class:`fiftyone.core.sample.SampleView` to which the
            frames are attached
    """

    def __init__(self, sample_view):
        super().__init__(sample_view)

        view = sample_view._view
        sf, ef = view._get_selected_excluded_fields(frames=True)
        ff = view._get_filtered_fields(frames=True)

        self._view = view
        self._selected_fields = sf
        self._excluded_fields = ef
        self._filtered_fields = ff

        self._needs_frames = view._needs_frames()
        self._contains_all_fields = view._contains_all_fields(frames=True)

    @property
    def field_names(self):
        return list(self._view.get_frame_field_schema().keys())

    @property
    def _frames_view(self):
        return self._view.select(self._sample.id)

    def add_frame(self, frame_number, frame, expand_schema=True):
        """Adds the frame to this instance.

        If an existing frame with the same frame number exists, it is
        overwritten.

        Unlike :class:`Frames.add_frame`, the provided frame is never modified
        in-place. Instead, a new :class:`FrameView` is constructed internally
        with the contents of the provided frame.

        Args:
            frame_number: the frame number
            frame: a :class:`Frame` or :class:`FrameView`
            expand_schema (True): whether to dynamically add new frame fields
                encountered to the dataset schema. If False, an error is raised
                if the frame's schema is not a subset of the dataset schema
        """
        fofu.validate_frame_number(frame_number)

        if not isinstance(frame, (Frame, FrameView)):
            raise ValueError(
                "Expected a %s or %s; found %s"
                % (Frame, FrameView, type(frame))
            )

        frame_view = self._make_frame({"_sample_id": self._sample._id})

        for field, value in frame.iter_fields():
            frame_view.set_field(field, value, create=expand_schema)

        frame_view.set_field("frame_number", frame_number)
        self._set_replacement(frame_view)

    def reload(self):
        """Reloads the view into the frames of the attached sample.

        Calling this method has the following effects:

        -   Clears the in-memory cache of :class:`FrameView` instances that you
            have loaded via this object. Any frames that you subsequently
            access will be loaded directly from the database

        -   Any additions, modifications, or deletions to frame views that you
            have loaded from this instance but not committed to the database by
            calling :meth:`save` will be discarded

        .. note::

            :class:`FrameView` objects are not singletons, so calling this
            method will not have any effect on :class:`FrameView` instances
            that you have **previously** loaded via this object

        Args:
            hard (False): whether to reload the frame schema in addition to the
                field values for the frames. This is necessary if new fields
                may have been added to the dataset's frame schema
        """
        self._delete_all = False
        self._delete_frames.clear()
        self._replacements.clear()

    def _get_frame_numbers_db(self):
        if not self._needs_frames:
            return super()._get_frame_numbers_db()

        pipeline = self._frames_view._pipeline(frames_only=True) + [
            {
                "$group": {
                    "_id": None,
                    "frame_numbers": {"$push": "$frame_number"},
                }
            }
        ]

        try:
            d = next(self._dataset._aggregate(pipeline))
            return set(d["frame_numbers"])
        except StopIteration:
            return set()

    def _get_frame_db(self, frame_number):
        if not self._needs_frames:
            return super()._get_frame_db(frame_number)

        pipeline = self._view._pipeline(frames_only=True)
        pipeline.append(
            {
                "$match": {
                    "_sample_id": self._sample._id,
                    "frame_number": frame_number,
                }
            }
        )

        try:
            return next(self._dataset._aggregate(pipeline))
        except StopIteration:
            return None

    def _iter_frames_db(self):
        if not self._needs_frames:
            return super()._iter_frames_db()

        return self._frames_view._aggregate(frames_only=True)

    def _make_frame(self, d):
        doc = self._dataset._frame_dict_to_doc(d)
        return FrameView(
            doc,
            self._view,
            selected_fields=self._selected_fields,
            excluded_fields=self._excluded_fields,
            filtered_fields=self._filtered_fields,
        )

    def _save_replacements(self):
        if not self._replacements:
            return

        if self._contains_all_fields:
            super()._save_replacements(include_singletons=False)
            return

        ops = []
        for frame_number, frame in self._replacements.items():
            doc = self._make_dict(frame)

            # Update elements of filtered array fields separately
            for field in self._filtered_fields:
                root, leaf = field.split(".", 1)
                for element in doc.pop(root, {}).get(leaf, []):
                    ops.append(
                        UpdateOne(
                            {
                                "frame_number": frame_number,
                                "_sample_id": self._sample._id,
                                field + "._id": element["_id"],
                            },
                            {"$set": {field + ".$": element}},
                        )
                    )

            # Update non-filtered fields
            ops.append(
                UpdateOne(
                    {
                        "frame_number": frame_number,
                        "_sample_id": self._sample._id,
                    },
                    {"$set": doc},
                    upsert=True,
                )
            )

        self._frame_collection.bulk_write(ops, ordered=False)
        self._replacements.clear()


class Frame(Document, metaclass=FrameSingleton):
    """A frame in a video :class:`fiftyone.core.sample.Sample`.

    Frames store all information associated with a particular frame of a video,
    including one or more sets of labels (ground truth, user-provided, or
    FiftyOne-generated) as well as additional features associated with subsets
    of the data and/or label sets.

    .. note::

        :class:`Frame` instances that are attached to samples **in datasets**
        are singletons, i.e.,  ``sample.frames[frame_number]`` will always
        return the same :class:`Frame` instance.

    Args:
        **kwargs: frame fields and values
    """

    _NO_DATASET_DOC_CLS = foo.NoDatasetFrameSampleDocument

    def _reload_backing_doc(self):
        if not self._in_db:
            return

        d = self._dataset._frame_collection.find_one(
            {"_sample_id": self._sample_id, "frame_number": self.frame_number}
        )
        self._doc = self._dataset._frame_dict_to_doc(d)


class FrameView(DocumentView):
    """A view into a :class:`Frame` in a video dataset.

    Like :class:`Frame` instances, the fields of a :class:`FrameView` instance
    can be modified, new fields can be created, and any changes can be saved to
    the database.

    :class:`FrameView` instances differ from :class:`Frame` instances in the
    following ways:

    -   A frame view may contain only a subset of the fields of its source
        frame, either by selecting and/or excluding specific fields
    -   A frame view may contain array fields or embedded array fields that
        have been filtered, thus containing only a subset of the array elements
        from the source frame
    -   Excluded fields of a frame view may not be accessed or modified

    .. note::

        :meth:`FrameView.save` will not delete any excluded fields or filtered
        array elements from the source frame.

    Frame views should never be created manually; they are generated when
    accessing the frames in a :class:`fiftyone.core.view.DatasetView`.

    Args:
        doc: a :class:`fiftyone.core.odm.frame.DatasetFrameSampleDocument`
        view: the :class:`fiftyone.core.view.DatasetView` that the frame
            belongs to
        selected_fields (None): a set of field names that this frame view is
            restricted to, if any
        excluded_fields (None): a set of field names that are excluded from
            this frame view, if any
        filtered_fields (None): a set of field names of list fields that are
            filtered in this frame view, if any
    """

    _DOCUMENT_CLS = Frame
