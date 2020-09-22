#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @Author: Niccolò Bonacchi
# @Date: Monday, September 7th 2020, 11:51:17 am
import json
import random
from pathlib import Path, PosixPath

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.pyplot import axvline

import alf.folders as folders
import alf.io
import ibllib.io.extractors.passive as passive
import ibllib.io.raw_data_loaders as rawio
from ibllib.io.extractors import ephys_fpga
from ibllib.plots import color_cycle, squares, vertical_lines
from oneibl.one import ONE

# hardcoded var
FRAME_FS = 60  # Sampling freq of the ipad screen, in Hertz
FS_FPGA = 30000  # Sampling freq of the neural recording system screen, in Hertz
NVALVE = 40  # number of expected valve clicks
NGABOR = 20 + 20 * 4 * 2  # number of expected Gabor patches
NTONES = 40
NNOISES = 40
DEBUG_PLOTS = False

dataset_types = [
    "_spikeglx_sync.times",
    "_spikeglx_sync.channels",
    "_spikeglx_sync.polarities",
    "_iblrig_RFMapStim.raw",
    "_iblrig_stimPositionScreen.raw",
    "_iblrig_syncSquareUpdate.raw",
    "ephysData.raw.meta",
    "_iblrig_taskSettings.raw",
    "_iblrig_taskData.raw",
]


# load session fixtures
def load_passive_session_fixtures(session_path):
    settings = rawio.load_settings(session_path)
    ses_nb = settings["SESSION_ORDER"][settings["SESSION_IDX"]]
    path_fixtures = Path(ephys_fpga.__file__).parent.joinpath("ephys_sessions")

    fixture = {
        "pcs": np.load(path_fixtures.joinpath(f"session_{ses_nb}_passive_pcs.npy")),
        "delays": np.load(path_fixtures.joinpath(f"session_{ses_nb}_passive_stimDelays.npy")),
        "ids": np.load(path_fixtures.joinpath(f"session_{ses_nb}_passive_stimIDs.npy")),
    }

    return fixture


def load_passive_stim_meta():
    path_fixtures = Path(ephys_fpga.__file__).parent.joinpath("ephys_sessions")
    with open(path_fixtures.joinpath("passive_stim_meta.json"), "r") as f:
        meta = json.load(f)

    return meta


def get_passive_spacers(session_path, sync=None, sync_map=None):
    """
    load and get spacer information, do corr to find spacer timestamps
    returns t_passive_starts, t_starts, t_ends
    """
    if sync is None or sync_map is None:
        sync, sync_map = ephys_fpga._get_main_probe_sync(session_path, bin_exists=False)

    meta = load_passive_stim_meta()
    t_end_ephys = passive.ephysCW_end(session_path=session_path)
    fttl = ephys_fpga._get_sync_fronts(sync, sync_map["frame2ttl"], tmin=t_end_ephys)
    spacer_template = (
        np.array(meta["VISUAL_STIM_0"]["ttl_frame_nums"], dtype=np.float32) / FRAME_FS
    )
    jitter = 3 / FRAME_FS  # allow for 3 screen refresh as jitter
    t_quiet = meta["VISUAL_STIM_0"]["delay_around"]
    spacer_times, _ = passive.get_spacer_times(
        spacer_template=spacer_template, jitter=jitter, ttl_signal=fttl["times"], t_quiet=t_quiet
    )

    # Check correct number of spacers found
    n_exp_spacer = np.sum(np.array(meta["STIM_ORDER"]) == 0)  # Hardcoded 0 for spacer
    if n_exp_spacer != np.size(spacer_times) / 2:
        raise ValueError(
            f"The number of expected spacer ({n_exp_spacer}) "
            f"is different than the one found on the raw "
            f"trace ({np.size(spacer_times)/2})"
        )

    spacer_times = np.r_[spacer_times.flatten(), sync["times"][-1]]
    return spacer_times[0], spacer_times[1::2], spacer_times[2::2]


# 1/3 Define start and end times of the 3 passive periods
def extract_passive_periods(session_path, sync=None, sync_map=None):
    if sync is None or sync_map is None:
        sync, sync_map = ephys_fpga._get_main_probe_sync(session_path, bin_exists=False)

    t_start_passive, t_starts, t_ends = get_passive_spacers(
        session_path, sync=sync, sync_map=sync_map
    )
    tspontaneous = [t_starts[0], t_ends[0]]
    trfm = [t_starts[1], t_ends[1]]
    treplay = [t_starts[2], t_ends[2]]
    # TODO export this to a dstype
    return t_start_passive, tspontaneous, trfm, treplay


# 2/3 RFMapping stimuli


# 3/3 Replay of task stimuli
def _extract_passiveGabor_df(fttl):
    # At this stage we want to define what pulses are and not quality control them.
    # Pulses are stricty altternating with intevals
    # find min max lengths for both (we don'tknow which are pulses and which are intervals yet)
    # remove first and last pulse
    diff0 = (np.min(np.diff(fttl["times"])[2:-2:2]), np.max(np.diff(fttl["times"])[2:-1:2]))
    diff1 = (np.min(np.diff(fttl["times"])[3:-2:2]), np.max(np.diff(fttl["times"])[3:-1:2]))
    # Highest max is of the intervals
    if max(diff0 + diff1) in diff0:
        thresh = diff0[0]
    elif max(diff0 + diff1) in diff1:
        thresh = diff1[0]
    # Anything lower than the min length of intervals is a pulse
    idx_start_stims = np.where((np.diff(fttl["times"]) < thresh) & (np.diff(fttl["times"]) > 0.1))[
        0
    ]

    idx_end_stims = idx_start_stims + 1

    start_times = fttl["times"][idx_start_stims]
    end_times = fttl["times"][idx_end_stims]
    # missing the first stim
    first_stim_off_idx = idx_start_stims[0] - 1
    first_stim_on_idx = first_stim_off_idx - 1
    if first_stim_on_idx <= 0:
        end_times = np.insert(end_times, 0, fttl["times"][first_stim_off_idx])
        start_times = np.insert(start_times, 0, end_times[0] - 0.3)

    # intervals dstype requires reshaping of start and end times
    passiveGabor_intervals = np.array([(x, y) for x, y in zip(start_times, end_times)])

    # Check length of presentation of stim is  within 150msof expected
    assert np.allclose(
        [y - x for x, y in passiveGabor_intervals], 0.3, atol=0.15
    ), "Stim length seems wrong"

    fixture = load_passive_session_fixtures(session_path)
    passiveGabor_properties = fixture["pcs"]
    # We can omit the assert for length as the expected length will be the len of the fixtures
    # append will fail if ngabor is not 180
    passiveGabor_table = np.append(passiveGabor_intervals, passiveGabor_properties, axis=1)
    columns = ["start", "stop", "position", "contrast", "phase"]
    passiveGabor_df = pd.DataFrame(passiveGabor_table, columns=columns)
    # TODO save dstype
    return passiveGabor_df


def _extract_passiveValve_intervals(bpod):
    # passiveValve.intervals
    # Get valve intervals from bpod channel
    # bpod channel should only contain valve output for passiveCW protocol
    # All high fronts == valve open times and low fronts == valve close times
    valveOn_times = bpod["times"][bpod["polarities"] > 0]
    valveOff_times = bpod["times"][bpod["polarities"] < 0]
    # TODO export this to a dstype

    assert len(valveOn_times) == NVALVE, "Wrong number of valve ONSET times"
    assert len(valveOff_times) == NVALVE, "Wrong number of valve OFFSET times"
    assert len(bpod["times"]) == NVALVE * 2, "Wrong number of valve FRONTS detected"  # (40 * 2)

    # check all values are within bpod tolerance of 100µs
    assert np.allclose(
        valveOff_times - valveOn_times, valveOff_times[0] - valveOn_times[0], atol=0.0001
    ), "Some valve outputs are longer or shorter than others"

    return np.array([(x, y) for x, y in zip(valveOn_times, valveOff_times)])


def _extract_passiveAudio_intervals(audio):
    # Get Tone and Noise cue intervals

    # Get all sound onsets and offsets
    soundOn_times = audio["times"][audio["polarities"] > 0]
    soundOff_times = audio["times"][audio["polarities"] < 0]
    # Check they are the correct number
    assert len(soundOn_times) == NTONES + NNOISES, "Wrong number of sound ONSETS"
    assert len(soundOff_times) == NTONES + NNOISES, "Wrong number of sound OFFSETS"

    diff = soundOff_times - soundOn_times
    # Tone is ~100ms so check if diff < 0.3
    toneOn_times = soundOn_times[diff < 0.3]
    toneOff_times = soundOff_times[diff < 0.3]
    # Noise is ~500ms so check if diff > 0.3
    noiseOn_times = soundOn_times[diff > 0.3]
    noiseOff_times = soundOff_times[diff > 0.3]
    # TODO export this to a dstype

    assert len(toneOn_times) == NTONES
    assert len(toneOff_times) == NTONES
    assert len(noiseOn_times) == NNOISES
    assert len(noiseOff_times) == NNOISES

    # Fixed delays from soundcard ~500µs
    np.allclose(toneOff_times - toneOn_times, 0.1, atol=0.0006)
    np.allclose(noiseOff_times - noiseOn_times, 0.5, atol=0.0006)

    passiveTone_intervals = np.append(
        toneOn_times.reshape((len(toneOn_times), 1)),
        toneOff_times.reshape((len(toneOff_times), 1)),
        axis=1,
    )
    passiveNoise_intervals = np.append(
        noiseOn_times.reshape((len(noiseOn_times), 1)),
        noiseOff_times.reshape((len(noiseOff_times), 1)),
        axis=1,
    )
    return passiveTone_intervals, passiveNoise_intervals


def extract_replay(session_path, treplay, sync=None, sync_map=None):
    if sync is None or sync_map is None:
        sync, sync_map = ephys_fpga._get_main_probe_sync(session_path, bin_exists=False)

    fttl = ephys_fpga._get_sync_fronts(sync, sync_map["frame2ttl"], tmin=treplay[0])
    passiveGabor_df = _extract_passiveGabor_df(fttl)

    bpod = ephys_fpga._get_sync_fronts(sync, sync_map["bpod"], tmin=treplay[0])
    passiveValve_intervals = _extract_passiveValve_intervals(bpod)

    audio = ephys_fpga._get_sync_fronts(sync, sync_map["audio"], tmin=treplay[0])
    passiveTone_intervals, passiveNoise_intervals = _extract_passiveAudio_intervals(audio)


def extract_replay_plot(session_path):
    # Load sessions sync channels, map
    sync, sync_map = ephys_fpga._get_main_probe_sync(session_path, bin_exists=False)

    f, ax = plt.subplots(1, 1)
    f.suptitle("/".join(session_path.split("/")[-5:]))
    plot_sync_channels(sync=sync, sync_map=sync_map, ax=ax)

    t_start_passive, t_starts, t_ends = get_passive_spacers(
        session_path, sync=sync, sync_map=sync_map
    )
    t_start_passive, tspontaneous, trfm, treplay = extract_passive_periods(
        session_path, sync=sync, sync_map=sync_map
    )
    plot_passive_periods(t_start_passive, t_starts, t_ends, ax=ax)

    fttl = ephys_fpga._get_sync_fronts(sync, sync_map["frame2ttl"], tmin=treplay[0])
    passiveGabor_df = _extract_passiveGabor_df(fttl)
    plot_gabor_times(passiveGabor_df, ax=ax)

    bpod = ephys_fpga._get_sync_fronts(sync, sync_map["bpod"], tmin=treplay[0])
    passiveValve_intervals = _extract_passiveValve_intervals(bpod)
    plot_valve_times(passiveValve_intervals, ax=ax)

    audio = ephys_fpga._get_sync_fronts(sync, sync_map["audio"], tmin=treplay[0])
    passiveTone_intervals, passiveNoise_intervals = _extract_passiveAudio_intervals(audio)
    plot_audio_times(passiveTone_intervals, passiveNoise_intervals, ax=ax)


def plot_sync_channels(sync, sync_map, ax=None):
    # Plot all sync pulses
    if ax is None:
        f, ax = plt.subplots(1, 1)
    for i, device in enumerate(["frame2ttl", "audio", "bpod"]):
        sy = ephys_fpga._get_sync_fronts(sync, sync_map[device])  # , tmin=t_start_passive)
        squares(sy["times"], sy["polarities"], yrange=[0.1 + i, 0.9 + i], color="k", ax=ax)


def plot_passive_periods(t_start_passive, t_starts, t_ends, ax=None):
    if ax is None:
        f, ax = plt.subplots(1, 1)
    # Update plot
    vertical_lines(
        np.r_[t_start_passive, t_starts, t_ends],
        ymin=-1,
        ymax=4,
        color=color_cycle(0),
        ax=ax,
        label="spacers",
    )
    ax.legend()


def plot_gabor_times(passiveGabor_df, ax=None):
    if ax is None:
        f, ax = plt.subplots(1, 1)
    # Update plot
    vertical_lines(
        passiveGabor_df["start"].values,
        ymin=0,
        ymax=1,
        color=color_cycle(1),
        ax=ax,
        label="GaborOn_times",
    )
    vertical_lines(
        passiveGabor_df["stop"].values,
        ymin=0,
        ymax=1,
        color=color_cycle(2),
        ax=ax,
        label="GaborOff_times",
    )
    ax.legend()


def plot_valve_times(passiveValve_intervals, ax=None):
    if ax is None:
        f, ax = plt.subplots(1, 1)
    # Update the plot
    vertical_lines(
        passiveValve_intervals[:, 0],
        ymin=2,
        ymax=3,
        color=color_cycle(3),
        ax=ax,
        label="ValveOn_times",
    )
    vertical_lines(
        passiveValve_intervals[:, 1],
        ymin=2,
        ymax=3,
        color=color_cycle(4),
        ax=ax,
        label="ValveOff_times",
    )
    ax.legend()


def plot_audio_times(passiveTone_intervals, passiveNoise_intervals, ax=None):
    if ax is None:
        f, ax = plt.subplots(1, 1)
    # Look at it
    vertical_lines(
        passiveTone_intervals[:, 0],
        ymin=1,
        ymax=2,
        color=color_cycle(5),
        ax=ax,
        label="toneOn_times",
    )
    vertical_lines(
        passiveTone_intervals[:, 1],
        ymin=1,
        ymax=2,
        color=color_cycle(6),
        ax=ax,
        label="toneOff_times",
    )
    vertical_lines(
        passiveNoise_intervals[:, 0],
        ymin=1,
        ymax=2,
        color=color_cycle(7),
        ax=ax,
        label="noiseOn_times",
    )
    vertical_lines(
        passiveNoise_intervals[:, 1],
        ymin=1,
        ymax=2,
        color=color_cycle(8),
        ax=ax,
        label="noiseOff_times",
    )

    ax.legend()
    # plt.show()


if __name__ == "__main__":
    # load data
    one = ONE()
    # eids = one.search(dataset_types=dataset_types)
    # for i, eid in enumerate(eids):
    #     one.load(eid, dataset_types=dataset_types, download_only=True)
    #     print(f"\n\n Finished downloading session: {i+1}/{len(eids)}\n\n")
    eids = [
        "c6db3304-c906-400c-aa0f-45dd3945b2ea",
        "88d24c31-52e4-49cc-9f32-6adbeb9eba87",
        "6fb1e12c-883b-46d1-a745-473cde3232c8",
        "83769b74-6e3b-422a-8648-50c1a32c5dd4",
        "695a6073-eae0-49e0-bb0f-e9e57a9275b9",
        "9cb1fb73-ab3f-488b-bcab-474994de38a8",
        "d86f3850-5183-4329-80ea-6902b9eb0e13",
        "f3ce3197-d534-4618-bf81-b687555d1883",
        "251ece37-7798-477c-8a06-2845d4aa270c",
        "aa20388b-9ea3-4506-92f1-3c2be84b85db",
        "c3d9b6fb-7fa9-4413-a364-92a54df0fc5d",
        "d23a44ef-1402-4ed7-97f5-47e9a7a504d9",
        "89e258e9-cbca-4eca-bac4-13a2388b5113",
        "5339812f-8b91-40ba-9d8f-a559563cc46b",
        "768a371d-7e88-47f8-bf21-4a6a6570dd6e",
        "eba834fd-74d7-4453-a11d-3ef4c7e55fe1",
        "62a5c50d-eaf4-4d48-b37d-b84cab0e90b3",
        "f49d972a-cf76-40c1-bf28-b83470ad6443",
        "d5a57a4c-d28b-4079-a549-abda2b9a00db",
        "0d8a7628-6c04-4d4b-bd99-95f2bda3e700",
        "308274fc-28e8-4bfd-a4e3-3903b7b48c28",
        "b3e335a4-3fe4-43cc-beb1-d3d3a802b03c",
        "6b82f9ef-bf10-42a8-b891-ef0d1fcc1593",
        "ebe2efe3-e8a1-451a-8947-76ef42427cc9",
        "edd22318-216c-44ff-bc24-49ce8be78374",
        "71e55bfe-5a3a-4cba-bdc7-f085140d798e",
        "49e0ab27-827a-4c91-bcaa-97eea27a1b8d",
        "5adab0b7-dfd0-467d-b09d-43cb7ca5d59c",
        "6527e2f1-8b2b-4b9b-a9dd-2a0206603ad8",
        "7f6b86f9-879a-4ea2-8531-294a221af5d0",
        "8c33abef-3d3e-4d42-9f27-445e9def08f9",
        "7b04526b-f0f7-41b9-93fd-d88d96c889b0",
        "f833e88a-fc3c-4cf5-80bb-ad0b41cc9053",
        "61e11a11-ab65-48fb-ae08-3cb80662e5d6",
        "eae2a292-d065-48f2-aca4-4da6832ea78f",
        "c7248e09-8c0d-40f2-9eb4-700a8973d8c8",
        "03063955-2523-47bd-ae57-f7489dd40f15",
        "1e45d992-c356-40e1-9be1-a506d944896f",
        "4d8c7767-981c-4347-8e5e-5d5fffe38534",
        "85d2b9d0-010b-470e-a00c-e13c5ca12fda",
        "41dfdc2a-987a-402a-99ae-779d5f569566",
        "252893e5-29fa-488e-b090-a48a4402fada",
        "e71bd25a-8c1e-4751-8985-4463a91c1b66",
        "fe1fd79f-b051-411f-a0a9-2530a02cc78d",
        "0fe99726-9982-4c41-a07c-2cd7af6a6733",
        "934dd7a4-fbdc-459c-8830-04fe9033bc28",
        "b39752db-abdb-47ab-ae78-e8608bbf50ed",
        "ee8b36de-779f-4dea-901f-e0141c95722b",
        "f9860a11-24d3-452e-ab95-39e199f20a93",
        "bd456d8f-d36e-434a-8051-ff3997253802",
        "b658bc7d-07cd-4203-8a25-7b16b549851b",
        "862ade13-53cd-4221-a3fa-dda8643641f2",
        "7622da34-51b6-4661-98ae-a57d40806008",
        "4720c98a-a305-4fba-affb-bbfa00a724a4",
        "66d98e6e-bcd9-4e78-8fbb-636f7e808b29",
        "f25642c6-27a5-4a97-9ea0-06652db79fbd",
        "b9003c82-6178-47ed-9ff2-75087a49c2f7",
        "77d2dfe0-a48d-4a46-8c13-2b9399c46ad3",
        "708e797c-5496-4742-9f00-1d6346768d7a",
        "8207abc6-6b23-4762-92b4-82e05bed5143",
        "ae384fe2-fa03-4366-a107-69b341b7368a",
        "1eed29f4-3311-4f54-8a6a-c2efa0675e4a",
        "14af98b4-f72b-46bf-ac06-97f61adc62cc",
        "7c0df410-ca82-4464-9de7-6e200b3a1d05",
        "8b1ad76f-7f0a-44fa-89f7-060be21c202e",
        "90e74228-fd1a-482f-bd56-05dbad132861",
        "6a601cc5-7b79-4c75-b0e8-552246532f82",
        "a82800ce-f4e3-4464-9b80-4c3d6fade333",
        "a66f1593-dafd-4982-9b66-f9554b6c86b5",
        "d855576e-5b34-41bf-8e3b-2bea0cae1380",
        "41431f53-69fd-4e3b-80ce-ea62e03bf9c7",
        "1eac875c-feaa-4a30-b148-059b954b11d8",
        "8db36de1-8f17-4446-b527-b5d91909b45a",
        "da188f2c-553c-4e04-879b-c9ea2d1b9a93",
        "03cf52f6-fba6-4743-a42e-dd1ac3072343",
        "cccee322-3163-44bb-9b4b-22083e96e09f",
        "fee6dd62-01d9-42d2-bcc1-5a7e27244edc",
        "37602f31-1b09-4fc9-87b8-cc13b922d0e6",
        "c90cdfa0-2945-4f68-8351-cb964c258725",
        "7be8fec4-406b-4e74-8548-d2885dcc3d5e",
        "ded7c877-49cf-46ad-b726-741f1cf34cef",
        "6364ff7f-6471-415a-ab9e-632a12052690",
        "af74b29d-a671-4c22-a5e8-1e3d27e362f3",
        "56d38157-bb5a-4561-ab5c-3df05a5d6e28",
        "9931191e-8056-4adc-a410-a4a93487423f",
        "e535fb62-e245-4a48-b119-88ce62a6fe67",
        "f10efe41-0dc0-44d0-8f26-5ff68dca23e9",
        "1191f865-b10a-45c8-9c48-24a980fd9402",
        "765ba913-eb0e-4f7d-be7e-4964abe8b27b",
        "6668c4a0-70a4-4012-a7da-709660971d7a",
        "37e96d0b-5b4b-4c6e-9b29-7edbdc94bbd0",
        "d16a9a8d-5f42-4b49-ba58-1746f807fcc1",
        "9a629642-3a9c-42ed-b70a-532db0e86199",
        "e5c772cd-9c92-47ab-9525-d618b66a9b5d",
        "dda5fc59-f09a-4256-9fb5-66c67667a466",
        "57b5ae8f-d446-4161-b439-b191c5e3e77b",
        "720a3fe6-5dfc-4a23-84f0-2f0b08e10ec2",
        "d2f5a130-b981-4546-8858-c94ae1da75ff",
        "7939711b-8b4d-4251-b698-b97c1eaa846e",
    ]
    session_paths = [
        "/home/nico/Downloads/FlatIron/hoferlab/Subjects/SWC_043/2020-09-20/001",
        "/home/nico/Downloads/FlatIron/hoferlab/Subjects/SWC_043/2020-09-19/001",
        "/home/nico/Downloads/FlatIron/hoferlab/Subjects/SWC_043/2020-09-18/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/CSP016/2020-09-18/001",
        "/home/nico/Downloads/FlatIron/hoferlab/Subjects/SWC_043/2020-09-17/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/CSP016/2020-09-17/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/CSP016/2020-09-16/001",
        "/home/nico/Downloads/FlatIron/hoferlab/Subjects/SWC_043/2020-09-15/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_016/2020-09-15/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_016/2020-09-14/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_016/2020-09-13/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_016/2020-09-12/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL024/2020-09-12/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_016/2020-09-11/001",
        "/home/nico/Downloads/FlatIron/mrsicflogellab/Subjects/SWC_039/2020-09-10/001",
        "/home/nico/Downloads/FlatIron/mrsicflogellab/Subjects/SWC_045/2020-08-28/001",
        "/home/nico/Downloads/FlatIron/mrsicflogellab/Subjects/SWC_045/2020-08-27/001",
        "/home/nico/Downloads/FlatIron/mrsicflogellab/Subjects/SWC_045/2020-08-26/001",
        "/home/nico/Downloads/FlatIron/mrsicflogellab/Subjects/SWC_045/2020-08-25/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL027/2020-08-20/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-26/2020-08-20/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_019/2020-08-18/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_019/2020-08-16/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-21/2020-08-14/005",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_019/2020-08-14/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-21/2020-08-13/002",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-21/2020-08-12/006",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-21/2020-08-11/002",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-21/2020-08-10/002",
        "/home/nico/Downloads/FlatIron/mainenlab/Subjects/ZM_3001/2020-08-07/001",
        "/home/nico/Downloads/FlatIron/mainenlab/Subjects/ZM_3001/2020-08-05/001",
        "/home/nico/Downloads/FlatIron/mrsicflogellab/Subjects/SWC_038/2020-08-01/001",
        "/home/nico/Downloads/FlatIron/mrsicflogellab/Subjects/SWC_038/2020-07-31/001",
        "/home/nico/Downloads/FlatIron/mrsicflogellab/Subjects/SWC_038/2020-07-30/001",
        "/home/nico/Downloads/FlatIron/mainenlab/Subjects/ZM_3003/2020-07-30/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL029/2020-07-29/001",
        "/home/nico/Downloads/FlatIron/mainenlab/Subjects/ZM_3003/2020-07-29/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL029/2020-07-28/001",
        "/home/nico/Downloads/FlatIron/mainenlab/Subjects/ZM_3003/2020-07-28/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL029/2020-07-27/001",
        "/home/nico/Downloads/FlatIron/mainenlab/Subjects/ZM_3003/2020-07-27/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_014/2020-07-19/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_014/2020-07-18/001",
        "/home/nico/Downloads/FlatIron/hoferlab/Subjects/SWC_042/2020-07-17/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_014/2020-07-17/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_014/2020-07-16/001",
        "/home/nico/Downloads/FlatIron/hoferlab/Subjects/SWC_042/2020-07-15/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_014/2020-07-15/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_014/2020-07-14/001",
        "/home/nico/Downloads/FlatIron/hoferlab/Subjects/SWC_042/2020-07-13/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_014/2020-07-13/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_021/2020-06-16/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_024/2020-06-11/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_024/2020-06-09/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_024/2020-06-08/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_017/2020-06-07/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_017/2020-06-05/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_017/2020-06-04/002",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_017/2020-06-03/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_022/2020-05-28/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_022/2020-05-26/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_022/2020-05-25/002",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_022/2020-05-24/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_022/2020-05-23/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_022/2020-05-22/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_022/2020-05-21/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_022/2020-05-20/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_022/2020-05-19/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_011/2020-03-23/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_011/2020-03-22/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-08/2020-03-20/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-08/2020-03-19/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-08/2020-03-18/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL_018/2020-03-16/002",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL060/2020-03-15/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL_019/2020-03-14/002",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL060/2020-03-14/003",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL_019/2020-03-13/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL060/2020-03-13/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL_019/2020-03-12/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_013/2020-03-12/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL060/2020-03-12/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL060/2020-03-11/004",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL060/2020-03-10/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_009/2020-03-09/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL059/2020-03-08/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL059/2020-03-07/002",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_013/2020-03-07/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_009/2020-03-07/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL059/2020-03-06/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL059/2020-03-05/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL059/2020-03-04/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL059/2020-03-03/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL045/2020-02-28/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-26/2020-08-21/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-26/2020-08-19/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-21/2020-08-19/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_019/2020-08-19/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-26/2020-08-18/001",
    ]
    failing = [
        "/home/nico/Downloads/FlatIron/mrsicflogellab/Subjects/SWC_045/2020-08-25/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL027/2020-08-20/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_019/2020-08-18/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_019/2020-08-14/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-21/2020-08-12/006",
        "/home/nico/Downloads/FlatIron/mainenlab/Subjects/ZM_3001/2020-08-05/001",
        "/home/nico/Downloads/FlatIron/mainenlab/Subjects/ZM_3003/2020-07-30/001",
        "/home/nico/Downloads/FlatIron/mainenlab/Subjects/ZM_3003/2020-07-29/001",
        "/home/nico/Downloads/FlatIron/mainenlab/Subjects/ZM_3003/2020-07-28/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL029/2020-07-27/001",
        "/home/nico/Downloads/FlatIron/mainenlab/Subjects/ZM_3003/2020-07-27/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_014/2020-07-13/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_017/2020-06-07/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_017/2020-06-05/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_022/2020-05-28/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_022/2020-05-26/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL060/2020-03-14/003",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL_019/2020-03-12/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_013/2020-03-12/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL060/2020-03-10/001",
        "/home/nico/Downloads/FlatIron/danlab/Subjects/DY_013/2020-03-07/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL059/2020-03-05/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL059/2020-03-04/001",
        "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL045/2020-02-28/001",
        "/home/nico/Downloads/FlatIron/zadorlab/Subjects/CSH_ZAD_019/2020-08-19/001",
        "/home/nico/Downloads/FlatIron/angelakilab/Subjects/NYU-26/2020-08-18/001",
    ]
    # eid = eids[random.randint(0, len(eids))]
    # print(eid)
    # session_paths = []
    # for i, eid in enumerate(eids):
    #     try:
    #         local_paths = one.load(eid, dataset_types=dataset_types, download_only=True)
    #         session_paths.append(alf.io.get_session_path(local_paths[0]))
    #         print(f"{i+1}/{len(eids)}")
    #     except BaseException as e:
    #         print(f"{i+1}/{len(eids)} - Failed session: {eid}")

    session_path = "/home/nico/Downloads/FlatIron/mrsicflogellab/Subjects/SWC_045/2020-08-25/001"

    session_path = session_paths[random.randint(0, len(eids))]
    session_path = "/home/nico/Downloads/FlatIron/churchlandlab/Subjects/CSHL060/2020-03-10/001"

    failed = []
    for i, sp in enumerate(session_paths):
        try:
            extract_replay_plot(sp)
        except BaseException:
            failed.append(sp)

        print(f"\n{i+1}/{len(session_paths)}")
