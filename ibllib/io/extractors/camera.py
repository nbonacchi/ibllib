""" Camera extractor functions
This module handles extraction of camera timestamps for both Bpod and FPGA.
"""
import logging
from pathlib import Path
from functools import partial

import cv2
import numpy as np
import matplotlib.pyplot as plt

from oneibl.stream import VideoStreamer
import ibllib.dsp.utils as dsp
from ibllib.plots import squares
from ibllib.io.video import assert_valid_label
from brainbox.behavior.wheel import within_ranges
from ibllib.io.extractors.base import get_session_extractor_type
from ibllib.io.extractors.ephys_fpga import _get_sync_fronts, get_main_probe_sync
import ibllib.io.raw_data_loaders as raw
from ibllib.io.extractors.base import (
    BaseBpodTrialsExtractor,
    BaseExtractor,
    run_extractor_classes,
)

_logger = logging.getLogger('ibllib')
PIN_STATE_THRESHOLD = 1


def extract_camera_sync(sync, chmap=None):
    """
    Extract camera timestamps from the sync matrix

    :param sync: dictionary 'times', 'polarities' of fronts detected on sync trace
    :param chmap: dictionary containing channel indices. Default to constant.
    :return: dictionary containing camera timestamps
    """
    # NB: should we check we opencv the expected number of frames ?
    assert(chmap)
    sr = _get_sync_fronts(sync, chmap['right_camera'])
    sl = _get_sync_fronts(sync, chmap['left_camera'])
    sb = _get_sync_fronts(sync, chmap['body_camera'])
    return {'right': sr.times[::2],
            'left': sl.times[::2],
            'body': sb.times[::2]}


def get_video_length(video_path):
    """
    Returns video length
    :param video_path: A path to the video
    :return:
    TODO Use get_video_meta with key arg instead?
    """
    is_url = isinstance(video_path, str) and video_path.startswith('http')
    cap = VideoStreamer(video_path).cap if is_url else cv2.VideoCapture(str(video_path))
    assert cap.isOpened(), f'Failed to open video file {video_path}'
    length = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return length


class CameraTimestampsFPGA(BaseExtractor):

    def __init__(self, label, session_path=None):
        super().__init__(session_path)
        self.label = assert_valid_label(label)
        self.save_names = f'_ibl_{label}Camera.times.npy'
        self.var_names = f'{label}_camera_timestamps'
        _logger.setLevel(logging.DEBUG)

    def __del__(self):
        _logger.setLevel(logging.INFO)

    def _extract(self, sync=None, chmap=None, video_path=None):
        """
        The raw timestamps are taken from the FPGA.  These are the times of the camera's frame
        TTLs.
        If the pin state file exists, these timestamps are aligned to the video frames using the
        audio TTLs.  Frames missing from the embedded frame count are removed from the timestamps
        array.
        If the pin state file does not exist, the left and right camera timestamps are aligned
        using the wheel data.
        :param sync:
        :param chmap:
        :return:
        """
        fpga_times = extract_camera_sync(sync=sync, chmap=chmap)
        count, gpio = raw.load_embedded_frame_data(self.session_path, self.label)

        if gpio is not None and gpio['indices'].size > 1:
            _logger.info('Aligning to audio TTLs')
            # Extract audio TTLs
            audio = _get_sync_fronts(sync, chmap['audio'])
            _, ts = raw.load_camera_ssv_times(self.session_path, self.label)
            """
            NB: Some of the audio TTLs occur very close together, and are therefore not 
            reflected in the pin state.  This function removes those.  Also converts frame times to
            FPGA time.
            """
            gpio, audio, ts = groom_pin_state(gpio, audio, ts)
            """
            The length of the count and pin state are regularly longer than the length of 
            the video file.  Here we assert that the video is either shorter or the same 
            length as the arrays, and  we make an assumption that the missing frames are 
            right at the end of the video.  We therefore simply shorten the arrays to match
            the length of the video.
            """
            if video_path is None:
                filename = f'_iblrig_{self.label}Camera.raw.mp4'
                video_path = self.session_path / 'raw_video_data' / filename
            length = get_video_length(video_path)
            if count.size > length:
                count = count[:length]
                # gpio = gpio[:length]
            else:
                assert length == count.size, 'fewer counts than frames'
                # _logger.warning('fewer frame counts than frames!')
            raw_ts = fpga_times[self.label]
            timestamps = align_with_audio(raw_ts, audio, gpio, count)
        else:
            _logger.warning('Alignment by wheel data not yet implemented')
            timestamps = fpga_times[self.label]

        return timestamps


class CameraTimestampsBpod(BaseBpodTrialsExtractor):
    """
    Get the camera timestamps from the Bpod

    The camera events are logged only during the events not in between, so the times need
    to be interpolated
    """
    save_names = '_ibl_leftCamera.times.npy'
    var_names = 'left_camera_timestamps'

    def _extract(self, video_path=None):
        ts = self._times_from_bpod()  # FIXME Extrapolate after alignment
        count, pin_state = raw.load_embedded_frame_data(self.session_path, 'left', raw=False)

        if pin_state is not None and any(pin_state):
            _logger.info('Aligning to audio TTLs')
            # Extract audio TTLs
            _, audio = raw.load_bpod_fronts(self.session_path, self.bpod_trials)
            # make sure that there are no 2 consecutive fall or consecutive rise events
            assert (np.all(np.abs(np.diff(audio['polarities'])) == 2))
            # make sure first TTL is high
            assert audio['polarities'][0] == 1
            return align_with_audio(ts, audio['times'][::2], pin_state, count)
        else:
            _logger.warning('Alignment by wheel data not yet implemented')

    def _times_from_bpod(self):
        ntrials = len(self.bpod_trials)

        cam_times = []
        n_frames = 0
        n_out_of_sync = 0
        for ind in np.arange(ntrials):
            # get upgoing and downgoing fronts
            pin = np.array(self.bpod_trials[ind]['behavior_data']
                           ['Events timestamps'].get('Port1In'))
            pout = np.array(self.bpod_trials[ind]['behavior_data']
                            ['Events timestamps'].get('Port1Out'))
            # some trials at startup may not have the camera working, discard
            if np.all(pin) is None:
                continue
            # if the trial starts in the middle of a square, discard the first downgoing front
            if pout[0] < pin[0]:
                pout = pout[1:]
            # same if the last sample is during an upgoing front,
            # always put size as it happens last
            pin = pin[:pout.size]
            frate = np.median(np.diff(pin))
            if ind > 0:
                """
                assert that the pulses have the same length and that we don't miss frames during
                the trial, the refresh rate of bpod is 100us
                """
                test1 = np.all(np.abs(1 - (pin - pout) / np.median(pin - pout)) < 0.1)
                test2 = np.all(np.abs(np.diff(pin) - frate) <= 0.00011)
                if not all([test1, test2]):
                    n_out_of_sync += 1
            # grow a list of cam times for ech trial
            cam_times.append(pin)
            n_frames += pin.size

        if n_out_of_sync > 0:
            _logger.warning(f"{n_out_of_sync} trials with bpod camera frame times not within"
                            f" 10% of the expected sampling rate")

        t_first_frame = np.array([c[0] for c in cam_times])
        t_last_frame = np.array([c[-1] for c in cam_times])
        frate = 1 / np.nanmedian(np.array([np.median(np.diff(c)) for c in cam_times]))
        intertrial_duration = t_first_frame[1:] - t_last_frame[:-1]
        intertrial_missed_frames = np.int32(np.round(intertrial_duration * frate)) - 1

        # initialize the full times array
        frame_times = np.zeros(n_frames + int(np.sum(intertrial_missed_frames)))
        ii = 0
        for trial, cam_time in enumerate(cam_times):
            if cam_time is not None:
                # populate first the recovered times within the trials
                frame_times[ii: ii + cam_time.size] = cam_time
                ii += cam_time.size
            if trial == (len(cam_times) - 1):
                break
            # then extrapolate in-between
            nmiss = intertrial_missed_frames[trial]
            frame_times[ii: ii + nmiss] = (cam_time[-1] + intertrial_duration[trial] /
                                           (nmiss + 1) * (np.arange(nmiss) + 1))
            ii += nmiss
        # import matplotlib.pyplot as plt
        # plt.plot(np.diff(frame_times))
        """
        if we find a video file, get the number of frames and extrapolate the times
         using the median frame rate as the video stops after the bpod
        """
        video_file, = list(self.session_path
                           .joinpath('raw_video_data')
                           .glob('_iblrig_leftCamera*.mp4'))
        if video_file:
            cap = cv2.VideoCapture(str(video_file))
            nframes = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            if nframes > len(frame_times):
                n_missing = int(nframes - frame_times.size)
                to_app = (np.arange(n_missing,) + 1) / frate + frame_times[-1]
                frame_times = np.r_[frame_times, to_app]
        assert all(np.diff(frame_times) > 0)  # negative diffs implies a big problem
        return frame_times


def align_with_audio(timestamps, audio, pin_state, count,
                     extrapolate_missing=True, display=False):
    """
    Groom the raw FPGA or Bpod camera timestamps using the frame embedded audio TTLs and frame
    counter.
    :param timestamps: An array of raw FPGA or Bpod camera timestamps
    :param audio: An array of FPGA or Bpod audio TTL times
    :param pin_state: An array of camera pin states
    :param count: An array of frame numbers
    :param extrapolate_missing: If true and the number of timestamps is fewer than the number of
    frame counts, the remaining timestamps are extrapolated based on the frame rate, otherwise
    they are NaNs
    :param display: Plot the resulting timestamps
    :return: The corrected frame timestamps
    """
    # Some assertions made on the raw data
    # assert count.size == pin_state.size, 'frame count and pin state size mismatch'
    assert all(np.diff(count) > 0), 'frame count not strictly increasing'
    assert all(np.diff(timestamps) > 0), 'FPGA camera times not strictly increasing'
    # low2high = np.diff(pin_state.astype(int)) == 1
    same_n_ttl = pin_state['times'].size == audio['times'].size
    assert same_n_ttl, 'more audio TTLs detected on camera than TTLs sent'

    """Here we will ensure that the FPGA camera times match the number of video frames in 
    length.  We will make the following assumptions: 

    1. The number of FPGA camera times is equal to or greater than the number of video frames.
    2. No TTLs were missed between the camera and FPGA.
    3. No pin states were missed by Bonsai.
    4  No pixel count data was missed by Bonsai.

    In other words the count and pin state arrays accurately reflect the number of frames 
    sent by the camera and should therefore be the same length, and the length of the frame 
    counter should match the number of saved video frames.

    The missing frame timestamps are removed in three stages:

    1. Remove any timestamps that occurred before video frame acquisition in Bonsai.
    2. Remove any timestamps where the frame counter reported missing frames, i.e. remove the
    dropped frames which occurred throughout the session.
    3. Remove the trailing timestamps at the end of the session if the camera was turned off
    in the wrong order.
    """
    # Align on first pin state change
    # first_uptick = (pin_state > 0).argmax()
    # first_ttl = np.searchsorted(timestamps, audio[0])
    first_uptick = pin_state['indices'][0]
    first_ttl = np.searchsorted(timestamps, audio['times'][0])
    """Here we find up to which index in the FPGA times we discard by taking the difference 
    between the index of the first pin state change (when the audio TTL was reported by the 
    camera) and the index of the first audio TTL in FPGA time.  We subtract the difference 
    between the frame count at the first pin state change and the index to account for any 
    video frames that were not saved during this period (we will remove those from the 
    camera FPGA times later).
    """
    # Minus any frames that were dropped between the start of frame acquisition and the
    # first TTL
    start = first_ttl - first_uptick - (count[first_uptick] - first_uptick)
    assert start >= 0

    # Remove the extraneous timestamps from the beginning and end
    end = count[-1] + 1 + start
    ts = timestamps[start:end]
    if ts.size < count.size:
        """
        For ephys sessions there may be fewer FPGA times than frame counts if SpikeGLX is turned 
        off before the video acquisition workflow.  For Bpod this always occurs because Bpod 
        finishes before the camera workflow.  For Bpod the times are already extrapolated for 
        these late frames."""
        n_missing = count.size - ts.size
        _logger.warning(f'{n_missing} fewer FPGA timestamps than frame counts')
        frate = round(1 / np.nanmedian(np.diff(ts)))
        to_app = ((np.arange(n_missing, ) + 1) / frate + ts[-1]
                  if extrapolate_missing
                  else np.full(n_missing, np.nan))
        ts = np.r_[ts, to_app]  # Append the missing times
    assert ts.size >= count.size
    assert ts.size == count[-1] + 1

    # Remove the rest of the dropped frames
    ts = ts[count]
    assert np.searchsorted(ts, audio['times'][0]) == first_uptick

    if display:
        # Plot to check
        import matplotlib.pyplot as plt
        from ibllib.plots import vertical_lines
        fig, axes = plt.subplots(2, 1)
        y = (pin_state > 0).astype(float)
        y *= 1e-5  # For scale when zoomed in
        axes[0].plot(ts, y, marker='d', color='blue', drawstyle='steps-pre')
        axes[0].plot(ts, np.zeros_like(ts), 'kx')
        vertical_lines(audio, ymin=0, ymax=1e-5, color='r', linestyle=':', ax=axes[0])
        # gpio_ttl_diff = ts[low2high] - audio[:sum(low2high)]
        # axes[1].hist(gpio_ttl_diff, 1000)
        # _logger.info('%i timestamps negative when taking diff between audio TTLs and GPIO',
        #              np.sum(gpio_ttl_diff < 0))

    return ts


def align_with_audio_safe(timestamps, audio, pin_state, count,
                          extrapolate_missing=True, display=False):
    """
    Groom the raw FPGA or Bpod camera timestamps using the frame embedded audio TTLs and frame
    counter.
    :param timestamps: An array of raw FPGA or Bpod camera timestamps
    :param audio: An array of FPGA or Bpod audio TTL times
    :param pin_state: An array of camera pin states
    :param count: An array of frame numbers
    :param extrapolate_missing: If true and the number of timestamps is fewer than the number of
    frame counts, the remaining timestamps are extrapolated based on the frame rate, otherwise
    they are NaNs
    :param display: Plot the resulting timestamps
    :return: The corrected frame timestamps
    """
    # Some assertions made on the raw data
    # assert count.size == pin_state.size, 'frame count and pin state size mismatch'
    assert all(np.diff(count) > 0), 'frame count not strictly increasing'
    assert all(np.diff(timestamps) > 0), 'FPGA camera times not strictly increasing'
    # low2high = np.diff(pin_state.astype(int)) == 1
    same_n_ttl = pin_state['times'].size == audio['times'].size
    assert same_n_ttl, 'more audio TTLs detected on camera than TTLs sent'

    """Here we will ensure that the FPGA camera times match the number of video frames in 
    length.  We will make the following assumptions: 

    1. The number of FPGA camera times is equal to or greater than the number of video frames.
    2. No TTLs were missed between the camera and FPGA.
    3. No pin states were missed by Bonsai.
    4  No pixel count data was missed by Bonsai.

    In other words the count and pin state arrays accurately reflect the number of frames 
    sent by the camera and should therefore be the same length, and the length of the frame 
    counter should match the number of saved video frames.

    The missing frame timestamps are removed in three stages:

    1. Remove any timestamps that occurred before video frame acquisition in Bonsai.
    2. Remove any timestamps where the frame counter reported missing frames, i.e. remove the
    dropped frames which occurred throughout the session.
    3. Remove the trailing timestamps at the end of the session if the camera was turned off
    in the wrong order.
    """
    # Align on first pin state change
    # first_uptick = (pin_state > 0).argmax()
    # first_ttl = np.searchsorted(timestamps, audio[0])
    first_uptick = pin_state['indices'][0]
    first_ttl = np.searchsorted(timestamps, audio['times'][0])
    """Here we find up to which index in the FPGA times we discard by taking the difference 
    between the index of the first pin state change (when the audio TTL was reported by the 
    camera) and the index of the first audio TTL in FPGA time.  We subtract the difference 
    between the frame count at the first pin state change and the index to account for any 
    video frames that were not saved during this period (we will remove those from the 
    camera FPGA times later).
    """
    # Minus any frames that were dropped between the start of frame acquisition and the
    # first TTL
    start = first_ttl - first_uptick - (count[first_uptick] - first_uptick)
    assert start >= 0

    # Remove the extraneous timestamps from the beginning and end
    end = count[-1] + 1 + start
    ts = timestamps[start:end]
    if ts.size < count.size:
        """
        For ephys sessions there may be fewer FPGA times than frame counts if SpikeGLX is turned
        off before the video acquisition workflow.  For Bpod this always occurs because Bpod
        finishes before the camera workflow.  For Bpod the times are already extrapolated for
        these late frames."""
        n_missing = count.size - ts.size
        _logger.warning(f'{n_missing} fewer FPGA timestamps than frame counts')
        frate = round(1 / np.nanmedian(np.diff(ts)))
        to_app = ((np.arange(n_missing, ) + 1) / frate + ts[-1]
                  if extrapolate_missing
                  else np.full(n_missing, np.nan))
        ts = np.r_[ts, to_app]  # Append the missing times
    assert ts.size >= count.size
    assert ts.size == count[-1] + 1

    # Remove the rest of the dropped frames
    ts = ts[count]
    assert np.searchsorted(ts, audio['times'][0]) == first_uptick

    return ts


def attribute_times(arr, events, tol=.1, injective=True, take='first'):
    """
    Returns the values of the first array that correspond to those of the second.

    Given two arrays of timestamps, the function will return the values of the first array
    that most likely correspond to the values of the second.  For each of the values in the
    second array, the absolute difference is taken and the index of either the first sufficiently
    close value, or simply the closest one, is assigned.

    If injective is True, once a value has been assigned, to a value it can't be assigned to
    another.  In other words there is a one-to-one mapping between the two arrays.

    :param arr: An array of event times to attribute to those in `events`
    :param events: An array of event times considered a subset of `arr`
    :param tol: The max absolute difference between values in order to be considered a match
    :param injective: If true, once a value has been assigned it will not be assigned again
    :param take: If 'first' the first value within tolerance is assigned; if 'nearest' the
    closest value is assigned
    :returns Numpy array the same length as `values`
    """
    take = take.lower()
    if take not in ('first', 'nearest'):
        raise ValueError('Parameter `take` must be either "first" or "nearest"')
    stack = np.ma.masked_invalid(arr, copy=False)
    stack.fill_value = np.inf
    assigned = np.full(events.shape, -1, dtype=int)  # Initialize output array
    for i, x in enumerate(events):
        dx = np.abs(stack.filled() - x)
        if dx.min() < tol:  # is any value within tolerance
            idx = np.where(dx < tol)[0][0] if take == 'first' else dx.argmin()
            assigned[i] = idx
            stack.mask[idx] = injective  # If one-to-one, remove the assigned value
    return assigned


def groom_pin_state(gpio, audio, ts, display=False):
    """
    Align the GPIO pin state to the FPGA audio TTLs.  Any audio TTLs not reflected in the pin
    state are removed from the dict and the times of the detected fronts are converted to FPGA time

    Note:
      - This function is ultra safe: we probably don't need assign all the ups and down fronts
      separately and could potentially even align the timestamps without removing the missed fronts
    :param gpio: array of GPIO pin state values
    :param audio: dict of FPGA audio TTLs (see ibllib.io.extractors.ephys_fpga._get_sync_fronts)
    :param ts: camera frame times
    :param display: If true, the resulting timestamps are plotted against the raw audio signal
    :returns: dict of GPIO FPGA front indices, polarities and FPGA aligned times
    :returns: audio times and polarities sans the TTLs not detected in the frame data
    :returns: frame times in FPGA time
    """
    TOL = 2  # Two pulses need to be within this many seconds to be considered related
    # Check that the dimensions match
    if np.any(gpio['indices'] >= ts.size):
        _logger.warning('GPIO events occurring beyond timestamps array length')
        keep = gpio['indices'] < ts.size
        gpio = {k: gpio[k][keep] for k, v in gpio.items()}
    assert audio['times'].size == audio['polarities'].size, 'audio data dimension mismatch'
    # make sure that there are no 2 consecutive fall or consecutive rise events
    assert (np.all(np.abs(np.diff(audio['polarities'])) == 2))
    # make sure first TTL is high
    assert audio['polarities'][0] == 1
    # make sure audio times in order
    assert np.all(np.diff(audio['times']) > 0)

    # make sure there are state changes
    assert gpio['indices'].any(), 'no TTLs detected in GPIO'
    # # make sure first GPIO state is high
    assert gpio['polarities'][0] == 1
    """
    Some audio TTLs appear to be so short that they are not recorded by the camera.  These can 
    be as short as a few microseconds.  Applying a cutoff based on framerate was unsuccessful.
    Assigning each audio TTL to each pin state change is not easy because some onsets occur very
    close together (sometimes < 70ms), on the order of the delay between TTL and frame time.
    Also, the two clocks have some degree of drift, so the delay between audio TTL and pin state
    change may be zero or even negative.

    Here we split the events into audio onsets (lo->hi) and audio offsets (hi->lo).  For each
    uptick in the GPIO pin state, we take the first audio onset time that was within 100ms of it. 
    We ensure that each audio TTL is assigned only once, so a TTL that is closer to frame 3 than
    frame 1 may still be assigned to frame 1.
    """
    ifronts = gpio['indices']  # The pin state flips
    if ifronts.size != audio['times'].size:
        _logger.warning('more audio TTLs than GPIO state changes, assigning timestamps')
        low2high = ifronts[gpio['polarities'] == 1]
        high2low = ifronts[gpio['polarities'] == -1]
        assert low2high.size >= high2low.size

        # Onsets
        ups = ts[low2high] - ts[low2high][0]
        onsets = audio['times'][::2] - audio['times'][0]
        assigned = attribute_times(onsets, ups, tol=TOL)
        unassigned = np.setdiff1d(np.arange(onsets.size), assigned[assigned > -1])
        if unassigned.size > 0:
            _logger.debug(f'{unassigned.size} audio TTL rises were not detected by the camera')
        # Check that all pin state upticks could be attributed to an onset TTL
        missed = assigned == -1
        if np.any(missed):
            _logger.warning(f'{sum(missed)} pin state rises could '
                            f'not be attributed to an audio TTL')
            assigned = assigned[~missed]
            if display:
                from ibllib.plots import vertical_lines
                ax = plt.subplot()
                vertical_lines(ups[assigned > -1],
                               linestyle='-', color='g', ax=ax,
                               label='assigned GPIO up state')
                vertical_lines(ups[missed],
                               linestyle='-', color='r', ax=ax,
                               label='unassigned GPIO up state')
                vertical_lines(onsets[unassigned],
                               linestyle=':', color='k', ax=ax,
                               alpha=0.3, label='audio onset')
                vertical_lines(onsets[assigned],
                               linestyle=':', color='b', ax=ax, label='assigned audio onset')
                plt.legend()
                plt.show()
        onsets_ = audio['times'][::2][assigned]

        # Offsets
        downs = ts[high2low] - ts[high2low][0]
        offsets = audio['times'][1::2] - audio['times'][1]
        assigned = attribute_times(offsets, downs, tol=TOL)
        unassigned = np.setdiff1d(np.arange(onsets.size), assigned[assigned > -1])
        if unassigned.size > 0:
            _logger.debug(f'{unassigned.size} audio TTL falls were not detected by the camera')
        # Check that all pin state downticks could be attributed to an offset TTL
        missed = assigned == -1
        if np.any(missed):
            _logger.warning(f'{sum(missed)} pin state falls could '
                            f'not be attributed to an audio TTL')
            assigned = assigned[~missed]
        offsets_ = audio['times'][1::2][assigned]

        # Audio groomed
        audio_ = {'times': np.empty(ifronts.size), 'polarities': gpio['polarities']}
        audio_['times'][::2] = onsets_
        audio_['times'][1::2] = offsets_
    else:
        audio_ = audio

    # Align the frame times to FPGA
    fcn_a2b, drift_ppm = dsp.sync_timestamps(ts[ifronts], audio_['times'])
    _logger.debug(f'frame audio alignment drift = {drift_ppm:.2f}ppm')
    # Add times to GPIO dict
    gpio['times'] = fcn_a2b(ts[ifronts])

    if display:
        # Plot all the onsets and offsets
        ax = plt.subplot()
        # GPIO
        x = np.insert(gpio['times'], 0, 0)
        y = np.arange(x.size) % 2
        squares(x, y, ax=ax, label='GPIO')
        y = within_ranges(np.arange(ts.size), ifronts.reshape(-1, 2))  # 0 or 1 for each frame
        ax.plot(fcn_a2b(ts), y, 'kx', label='cam times')
        # Audio
        squares(audio['times'], audio['polarities'],
                ax=ax, label='audio TTL', linestyle=':', color='r', yrange=[0, 1])
        ax.legend()
        plt.xlabel('FPGA time (s)')
        ax.set_yticks([0, 1])
        ax.set_title('GPIO - audio TTL alignment')
        plt.show()

    return gpio, audio_, fcn_a2b(ts)


def extract_all(session_path, session_type=None, save=True, **kwargs):
    """
    For the IBL ephys task, reads ephys binary file and extract:
        -   video time stamps
    :param session_path: '/path/to/subject/yyyy-mm-dd/001'
    :param session_type: the session type to extract, i.e. 'ephys', 'training' or 'biased'. If
    None the session type is inferred from the settings file.
    :param save: Bool, defaults to False
    :param kwargs: parameters to pass to the extractor
    :return: outputs, files
    """
    if session_type is None:
        session_type = get_session_extractor_type(session_path)
    if session_type == 'ephys':
        labels = assert_valid_label(kwargs.pop('labels', ('left', 'right', 'body')))
        labels = (labels,) if isinstance(labels, str) else labels  # Ensure list/tuple
        extractor = [partial(CameraTimestampsFPGA, label) for label in labels]
        if 'sync' not in kwargs:
            kwargs['sync'], kwargs['chmap'] = \
                get_main_probe_sync(session_path, bin_exists=kwargs.pop('bin_exists', False))
    elif session_type in ['biased', 'training']:
        assert kwargs.pop('labels', 'left'), 'only left camera is currently supported'
        extractor = CameraTimestampsBpod
    else:
        raise ValueError(f"Session type {session_type} as no matching extractor {session_path}")

    outputs, files = run_extractor_classes(
        extractor, session_path=session_path, save=save, **kwargs)
    return outputs, files