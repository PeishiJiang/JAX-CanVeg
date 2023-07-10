"""
One-dimensional column-based hydrobiogeochemical modeling, including:
- surface/subsurface energy balance;
- surface/subsurface water mass balance;
- surface/subsurface carbon/nutrient cycle.

Author: Peishi Jiang
Date: 2023.07.06.
"""

# TODO: Let's first write it in a way that directly takes in the example forcing data.
# Further modifications are needed.

import jax
import jax.numpy as jnp

# import matplotlib.pyplot as plt

# from jax_watershed.physics.energy_fluxes.surface_energy import solve_surface_energy
from jax_canoak.physics.energy_fluxes import (
    # rnet,
    par,
    nir,
    gfunc,
    diffuse_direct_radiation,
    irflux,
)
from jax_canoak.physics.carbon_fluxes import angle, lai_time, stomata

from jax_canoak.shared_utilities.forcings import Alf_forcings_30min as forcings
from jax_canoak.shared_utilities.forcings import Alf_divergence as divergence_matrix
from jax_canoak.shared_utilities.forcings import get_input_t
from jax_canoak.shared_utilities.domain import Time

# from jax_canoak.subjects import Surface, Soil

# TODO: Check the data types!
# Set float64
# from jax.config import config
# jax.config.update("jax_enable_x64", True)

plotting = True

# ---------------------------------------------------------------------------- #
#                     Model parameter/properties settings                      #
# ---------------------------------------------------------------------------- #
# Spatio-temporal information/discretizations
t0, tn, dt = 181.0, 194.0, 1.0 / 48.0  # [day]
latitude, longitude, zone = 38.1, -121.65, -8

# Subsurface layers
z0, zn, nz = 0.0, 10.0, 21  # subsurface layers [m]

# Canopy layers
ht = 1.0  # canopy height [m]
jtot = 30  # number of canopy layers
sze, jktot = jtot + 2, jtot + 1
delz = ht / jtot  # height of each layer
zh65 = 0.65 / ht
pai = 0.0  # plant area index
# height of mid point of layer scaled
ht_midpt = jnp.array([0.5, 1.0, 1.5, 2.0, 2.5])
lai_freq_scaled = jnp.array([0.6, 0.6, 0.6, 0.6, 0.6])
# lai_freq_np = np.array([0.05, .3,.3, .3, .05]) * lai

# Domain layers
jtot3 = 150  # number of layers in the domain, three times canopy height
sze3 = jtot3 + 2
izref = jtot3 - 1  # array index of reference ht

# Optical properties of PAR and NIR
par_reflect, par_trans, par_soil_refl = 0.0377, 0.072, 0.0
nir_reflect, nir_trans, nir_soil_refl = 0.6, 0.26, 0.0
par_absorbed = 1 - par_reflect - par_trans
nir_absorbed = 1 - nir_reflect - nir_trans
ratradnoon = 0.0

# Surface characteristics
# pft_ind = 10
# f_snow, f_cansno = 0.0, 0.0
# z_a, z0m, z0c, d = 5., 0.05, 0.05, 0.05
# gsoil, gstomatal = 1e10, 1.0 / 180.0

# Subsurface characteristics
# κ = 0.05
# dz_soil1 = z0


# ---------------------------------------------------------------------------- #
#                               Read forcing data                              #
# ---------------------------------------------------------------------------- #
# Atmospheric forcings
forcing_list = forcings.varn_list
rg_ind, pa_ind, lai_ind = (
    forcing_list.index("Rg"),
    forcing_list.index("PA"),
    forcing_list.index("LAI"),
)

# Check the shape of Thomson dispersion matrix
if divergence_matrix.shape != (jtot, jtot3):
    raise Exception(
        "The shape of the divergence matrix is not identical to the domain size!"
    )

# ---------------------------------------------------------------------------- #
#                            Initialize the subjects                           #
# ---------------------------------------------------------------------------- #
time = Time(t0=t0, tn=tn, dt=dt, start_time="2018-01-01 00:00:00")
# soil_column = Column(xs=jnp.linspace(z0, zn, nz))
# Δz = soil_column.Δx
# surface = Surface(ts=time, space=Column(xs=soil_column.xs[:2]))
# soil = Soil(ts=time, space=soil_column, κ=None)


# ---------------------------------------------------------------------------- #
#                     Numerically solve the model over time                    #
# ---------------------------------------------------------------------------- #
t_prev, t_now = t0, t0 + dt  # t_now == t_prev for the initial step
tind_prev, tind_now = 0, 0

# JIT the functions
par, nir, gfunc = jax.jit(par), jax.jit(nir), jax.jit(gfunc)
diffuse_direct_radiation = jax.jit(diffuse_direct_radiation)
angle, lai_time = jax.jit(angle), jax.jit(lai_time, static_argnames=["sze"])

while t_now < tn:
    # ------------------------- Get the current time step ------------------------ #
    t_now_fmt = time.return_formatted_time(t_now)
    year, day = t_now_fmt.year, t_now_fmt.timetuple().tm_yday
    hour = t_now_fmt.hour + t_now_fmt.minute / 60.0

    # Get the forcing data
    (
        rglobal,
        parin,
        press_kpa,
        lai,
        ta,
        ws,
        ustar,
        co2,
        ea,
        ts,
        swc,
        T_Kelvin,
        rhova_g,
        rhova_kg,
        relative_humidity,
        vpd,
        press_bars,
        press_Pa,
        pstat273,
        gcut,
        rcuticle,
        air_density,
        air_density_mole,
        soil_Tave_15cm,
        heatcoef,
    ) = get_input_t(forcings, t_now)
    # jax.debug.print("rglobal: {a}; parin: {b}", a=rglobal, b=parin)

    print(f"Time: {day} day; Hour: {hour}.")
    # if (day==182) and (hour>8):
    #     exit()

    # ----------------------------- Evolve the model ----------------------------- #
    # Perform some initializations
    sun_tleaf, shd_tleaf = jnp.ones(sze) * ta, jnp.ones(sze) * ta
    sun_T_filter, shd_T_filter = jnp.ones(sze) * ta, jnp.ones(sze) * ta
    tair, tair_filter = jnp.ones(sze3) * ta, jnp.ones(sze3) * ta
    rhov_air, rhov_filter = jnp.ones(sze3) * rhova_kg, jnp.ones(sze3) * rhova_kg
    co2_air = jnp.ones(sze3) * co2
    sfc_temperature = ta

    # Update LAI structure with new day
    exxpdir, dLAIdz, Gfunc_sky = lai_time(sze, lai, ht, ht_midpt, lai_freq_scaled)

    # Compute solar elevation angle
    solar_beta_rad, solar_sine_beta, solar_beta_deg = angle(
        latitude, longitude, zone, year, day, hour
    )

    # Make sure PAR is zero at night
    parin = jax.lax.cond(solar_sine_beta <= 0.01, lambda: 0.0, lambda: parin)

    # Compute the fractions of beam and diffusion radiation from incoming measurements
    ratrad, par_beam, par_diffuse, nir_beam, nir_diffuse = jax.lax.cond(
        solar_sine_beta > 0.05,
        lambda x: diffuse_direct_radiation(x[0], x[1], x[2], x[3]),
        lambda x: (ratradnoon, 0.0, 0.0, 0.0, 0.0),
        [solar_sine_beta, rglobal, parin, press_kpa],
    )
    ratradnoon = jax.lax.cond(
        (hour > 12) & (hour < 13), lambda: ratrad, lambda: ratradnoon
    )

    # Comptue leaf inclination angle distributionf function, the mean direction cosine
    # between the sun zenith angle and the angle normal to the mean leaf
    Gfunc_solar = jax.lax.cond(
        solar_sine_beta >= 0.01,
        lambda x: gfunc(x[0], x[1]),
        lambda x: jnp.zeros(sze),
        [solar_beta_rad, dLAIdz],
    )

    # Compute PAR profiles
    (
        sun_lai,
        shd_lai,
        prob_beam,
        prob_sh,
        par_up,
        par_down,
        beam_flux_par,
        quantum_sh,
        quantum_sun,
        par_shade,
        par_sun,
    ) = par(
        solar_sine_beta,
        parin,
        par_beam,
        par_reflect,
        par_trans,
        par_soil_refl,
        par_absorbed,
        dLAIdz,
        exxpdir,
        Gfunc_solar,
    )

    # Compute NIR profiles
    nir_dn, nir_up, beam_flux_nir, nir_sh, nir_sun = nir(
        solar_sine_beta,
        nir_beam,
        nir_diffuse,
        nir_reflect,
        nir_trans,
        nir_soil_refl,
        nir_absorbed,
        dLAIdz,
        exxpdir,
        Gfunc_solar,
    )

    # Compute stomatal conductance for sunlit and
    # shaded leaf fractions as a function of light
    # on those leaves.
    sun_rs_jnp, shd_rs_jnp = stomata(
        lai,
        pai,
        rcuticle,
        par_sun,
        par_shade,
    )

    # Compute probability of penetration for diffuse
    # radiation for each layer in the canopy
    ir_up, ir_dn = irflux(
        T_Kelvin,
        ratrad,
        sfc_temperature,
        exxpdir,
        sun_T_filter,
        shd_T_filter,
        prob_beam,
        prob_sh,
    )

    jax.debug.print("ir_up: {a}; ir_dn: {b}", a=ir_up, b=ir_dn)
    # jax.debug.print("nir_up: {a}", a=nir_up)
    # jax.debug.print(
    #     "nir_beam: {a}; solar_sine_beta: {b}", a=nir_beam,
    #     b=solar_sine_beta
    # )

    # Iteration looping for energy fluxes and scalar fields
    # iterate until energy balance closure occurs or 75 iterations are reached

    # Update the time step
    t_prev = t_now
    t_now = min(t_now + dt, tn)

    # Update the time indices
    tind_prev = tind_now
    tind_now = min(tind_now + 1, time.nt)


# if plotting:
#     fig, axes = plt.subplots(2, 2, figsize=(10, 10), sharex=False)
#     ax = axes[0,0]
#     ax.plot(time.t_list, surface.states["T_a"], label="T_a")
#     ax.plot(time.t_list, surface.states["T_v"], label="T_v")
#     ax.plot(time.t_list, surface.states["T_g"], label="T_g")
#     ax.set(ylabel="[degK]", xlabel="Time [day]",title="Air/Canopy/Ground Temperature")
#     ax.legend()

#     ax = axes[1,0]
#     ax.plot(time.t_list, surface.states["l"], label="l")
#     ax.set(ylabel="[m]",xlabel="Time [day]",title="Obukhov length",yscale='symlog')

#     ax = axes[0,1]
#     # ax.plot(surface.states["L_v"], "g--", label="L_v")
#     # ax.plot(surface.states["L_g"], "g", label="L_g")
#     # ax.plot(surface.states["H_v"], "r--", label="H_v")
#     # ax.plot(surface.states["H_g"], "r", label="H_g")
#     # ax.plot(λ * surface.states["E_v"], "b--", label="λE_v")
#     # ax.plot(λ * surface.states["E_g"], "b", label="λE_g")
#     # ax.plot(surface.states["L_v"]+surface.states["L_g"], "g", label="L")
#     ax.plot(time.t_list, surface.states["H_v"]+surface.states["H_g"], "r", label="H")
#     ax.plot(time.t_list,λ*surface.states["E_v"]+λ*surface.states["E_g"],"b",label="E")
#     # ax.plot(surface.states["G"], color="black", label="G")
#     ax.set(ylabel="[W m-2]", xlabel="Time [day]", title="Heat fluxes",)
#         #    ylim=[-150, 150])
#     ax.legend(ncols=2)

#     ax = axes[1,1]
#     im = ax.imshow(soil.states["Tsoil"].T, aspect="auto")
#     ax.set(xlabel="Time [hr]", ylabel="Soil depth", title="Soil temperature")
#     cbar = fig.colorbar(im, orientation="horizontal")
#     cbar.ax.set(xlabel="degK")

#     # plt.savefig("1d_column_energy.png")
#     plt.show()
