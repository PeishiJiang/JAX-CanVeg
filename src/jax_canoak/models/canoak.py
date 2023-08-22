"""
This is a big jax function for running canoak, given the inputs.

Author: Peishi Jiang
Date: 2023.8.13.
"""

import jax
import jax.numpy as jnp
import equinox as eqx

from typing import Tuple

from ..shared_utilities.types import Float_2D
from ..subjects import Para, Met, Prof, SunAng, LeafAng, SunShadedCan
from ..subjects import Setup, Veg, Soil, Rnet, Qin, Ir, ParNir, Lai, Can
from ..subjects import initialize_profile, initialize_model_states

from ..shared_utilities.utils import dot
from ..subjects import update_profile, calculate_veg
from ..physics import energy_carbon_fluxes

# from jax_canoak.physics.energy_fluxes import rad_tran_canopy, sky_ir_v2
from ..physics.energy_fluxes import rad_tran_canopy, sky_ir
from ..physics.energy_fluxes import compute_qin, ir_rad_tran_canopy
from ..physics.energy_fluxes import uz, soil_energy_balance
from ..physics.energy_fluxes import diffuse_direct_radiation
from ..physics.carbon_fluxes import angle, leaf_angle
from ..physics.carbon_fluxes import soil_respiration_alfalfa


def canoak(
    para: Para,
    setup: Setup,
    met: Met,
    dij: Float_2D,
    soil_mtime: int,
    niter: int = 15,
) -> Tuple[
    Met,
    Prof,
    ParNir,
    ParNir,
    Ir,
    Rnet,
    Qin,
    SunAng,
    LeafAng,
    Lai,
    SunShadedCan,
    SunShadedCan,
    Soil,
    Veg,
    Can,
]:
    # ---------------------------------------------------------------------------- #
    #                     Initialize profiles of scalars/sources/sinks             #
    # ---------------------------------------------------------------------------- #
    prof = initialize_profile(met, para, setup)
    ntime, jtot = prof.H.shape

    # ---------------------------------------------------------------------------- #
    #                     Initialize model states                        #
    # ---------------------------------------------------------------------------- #
    soil, quantum, nir, ir, qin, rnet, sun, shade, veg, lai = initialize_model_states(
        met, para, setup
    )

    # ---------------------------------------------------------------------------- #
    #                     Compute sun angles                                       #
    # ---------------------------------------------------------------------------- #
    sun_ang = angle(setup.lat_deg, setup.long_deg, setup.time_zone, met.day, met.hhour)

    # ---------------------------------------------------------------------------- #
    #                     Compute leaf angle                                       #
    # ---------------------------------------------------------------------------- #
    leaf_ang = leaf_angle(sun_ang, para, setup, lai)

    # ---------------------------------------------------------------------------- #
    #                     Compute direct and diffuse radiations                    #
    # ---------------------------------------------------------------------------- #
    ratrad, par_beam, par_diffuse, nir_beam, nir_diffuse = diffuse_direct_radiation(
        sun_ang.sin_beta, met.rglobal, met.parin, met.P_kPa
    )
    quantum = eqx.tree_at(
        lambda t: (t.inbeam, t.indiffuse), quantum, (par_beam, par_diffuse)
    )
    nir = eqx.tree_at(lambda t: (t.inbeam, t.indiffuse), nir, (nir_beam, nir_diffuse))

    # ---------------------------------------------------------------------------- #
    #                     Initialize IR fluxes with air temperature                #
    # ---------------------------------------------------------------------------- #
    ir_in = sky_ir(met.T_air_K, ratrad, para.sigma)
    # ir_in = sky_ir_v2(met, ratrad, para.sigma)
    ir_dn = dot(ir_in, ir.ir_dn)
    ir_up = dot(ir_in, ir.ir_up)
    ir = eqx.tree_at(lambda t: (t.ir_in, t.ir_dn, t.ir_up), ir, (ir_in, ir_dn, ir_up))

    # ---------------------------------------------------------------------------- #
    #                     Compute radiation fields             #
    # ---------------------------------------------------------------------------- #
    # PAR
    quantum = rad_tran_canopy(
        sun_ang,
        leaf_ang,
        quantum,
        para,
        lai,
        para.par_reflect,
        para.par_trans,
        para.par_soil_refl,
        niter=5,
    )
    # NIR
    nir = rad_tran_canopy(
        sun_ang,
        leaf_ang,
        nir,
        para,
        lai,
        para.nir_reflect,
        para.nir_trans,
        para.nir_soil_refl,
        niter=25,
    )  # noqa: E501

    # ---------------------------------------------------------------------------- #
    #                     Iterations                                               #
    # ---------------------------------------------------------------------------- #
    # compute Tsfc -> IR -> Rnet -> Energy balance -> Tsfc
    # loop again and apply updated Tsfc info until convergence
    # This is where things should be jitted as a whole
    def iteration(c, i):
        met, prof, ir, qin, sun, shade, soil, veg = c
        # jax.debug.print("T soil: {a}", a=soil.T_soil[10,:])
        # jax.debug.print("T sfc: {a}", a=soil.sfc_temperature[10])
        # jax.debug.print("Tsfc: {a}", a=prof.Tair_K.mean())
        # jax.debug.print("T soil surface: {a}", a=soil.sfc_temperature.mean())

        # Update canopy wind profile with iteration of z/l and use in boundary layer
        # resistance computations
        wind = uz(met, para, setup.n_can_layers)
        prof = eqx.tree_at(lambda t: t.wind, prof, wind)

        # Compute IR fluxes with Bonan's algorithms of Norman model
        ir = ir_rad_tran_canopy(leaf_ang, ir, quantum, soil, sun, shade, para)

        # Incoming short and longwave radiation
        qin = compute_qin(quantum, nir, ir, para, qin)

        # Compute energy fluxes for H, LE, gs, A on Sun and Shade leaves
        # Compute new boundary layer conductances based on new leaf energy balance
        # and delta T, in case convection occurs
        # Different coefficients will be assigned if amphistomatous or hypostomatous
        sun, shade = energy_carbon_fluxes(
            sun, shade, qin, quantum, met, prof, para, setup
        )

        # Compute soil fluxes
        soil = soil_energy_balance(quantum, nir, ir, met, prof, para, soil, soil_mtime)  # type: ignore  # noqa: E501

        # Compute soil respiration
        soil_resp = soil_respiration_alfalfa(
            veg.Ps, soil.T_soil[:, 9], met.soilmoisture, met.zcanopy, veg.Rd, para
        )
        soil = eqx.tree_at(lambda t: t.resp, soil, soil_resp)

        # Compute profiles of C's, zero layer jtot+1 as that is not a dF/dz or
        # source/sink level
        prof = update_profile(met, para, prof, quantum, sun, shade, soil, veg, lai, dij)

        # compute met.zL from HH and met.ustar
        HH = jnp.sum(
            (
                quantum.prob_beam[:, :jtot] * sun.H
                + quantum.prob_shade[:, :jtot] * shade.H
            )
            * lai.dff[:, :jtot],
            axis=1,
        )
        zL = -(0.4 * 9.8 * HH * para.meas_ht) / (
            met.air_density * 1005 * met.T_air_K * jnp.power(met.ustar, 3.0)
        )
        zL = jnp.clip(zL, a_min=-3, a_max=0.25)
        met = eqx.tree_at(lambda t: t.zL, met, zL)

        # Compute canopy integrated fluxes
        veg = calculate_veg(para, lai, quantum, sun, shade)

        cnew = [met, prof, ir, qin, sun, shade, soil, veg]
        return cnew, None

    initials = [met, prof, ir, qin, sun, shade, soil, veg]
    finals, _ = jax.lax.scan(iteration, initials, xs=None, length=niter)

    met, prof, ir, qin, sun, shade, soil, veg = finals

    # Calculate the states/fluxes across the whole canopy
    rnet_calc = (
        quantum.beam_flux[:, jtot] / 4.6
        + quantum.dn_flux[:, jtot] / 4.6
        - quantum.up_flux[:, jtot] / 4.6
        + nir.beam_flux[:, jtot]
        + nir.dn_flux[:, jtot]
        - nir.up_flux[:, jtot]
        + ir.ir_dn[:, jtot]
        + -ir.ir_up[:, jtot]
    )
    LE = veg.LE + soil.evap
    H = veg.H + soil.heat
    rnet = veg.Rnet + soil.rnet
    NEE = soil.resp - veg.GPP
    avail = rnet_calc - soil.gsoil
    gsoil = soil.gsoil
    albedo_calc = (quantum.up_flux[:, jtot] / 4.6 + nir.up_flux[:, jtot]) / (
        quantum.incoming / 4.6 + nir.incoming
    )
    nir_albedo_calc = nir.up_flux[:, jtot] / nir.incoming
    nir_refl = nir.up_flux[:, jtot] - nir.up_flux[:, 0]

    can = Can(
        rnet_calc,
        rnet,
        LE,
        H,
        NEE,
        avail,
        gsoil,
        albedo_calc,
        nir_albedo_calc,
        nir_refl,
    )

    return (
        met,
        prof,
        quantum,
        nir,
        ir,
        rnet,
        qin,
        sun_ang,
        leaf_ang,
        lai,
        sun,
        shade,
        soil,
        veg,
        can,
    )
