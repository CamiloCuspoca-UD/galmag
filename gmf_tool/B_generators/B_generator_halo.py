# -*- coding: utf-8 -*-
from gmf_tool.B_field import B_field_component
import numpy as np
from B_generator import B_generator
from gmf_tool.Grid import Grid
import halo_free_decay_modes
from util import curl_spherical, simpson
from gmf_tool.halo_profiles import simple_V, simple_alpha

class B_generator_halo(B_generator):
    def __init__(self, grid=None, box=None, resolution=None,
                 grid_type='cartesian', default_parameters={},
                 dtype=np.float):

        super(B_generator_halo, self).__init__(
                                        grid=grid,
                                        box=box,
                                        resolution=resolution,
                                        grid_type=grid_type,
                                        default_parameters=default_parameters,
                                        dtype=dtype)

        self.growth_rate = np.NaN
        self.component_count = 0

    @property
    def _builtin_parameter_defaults(self):
        builtin_defaults = {
                            'halo_symmetric_field': True,
                            'halo_n_free_decay_modes': 4,
                            'halo_dynamo_type': 'alpha-omega',
                            'halo_rotation_function': simple_V,
                            'halo_alpha_function': simple_alpha,
                            'halo_Galerkin_ngrid': 501,
                            'halo_growing_mode_only': False,
                            'halo_compute_only_one_quadrant': True,
                            'halo_turbulent_induction': 0.6,
                            'halo_rotation_induction': 200.0,
                            'halo_radius': 20.0,
                            'halo_ref_radius': 8.5, # kpc (approx. solar radius)
                            'halo_ref_z': 0.02, # kpc (approx. solar z)
                            'halo_ref_Bphi': 0.1 # \muG
                            }
        return builtin_defaults


    def get_B_field(self, **kwargs):
        """
        Constructs a B_field component object containing a solution of the
        dynamo equation for the halo field.

        """
        parsed_parameters = self._parse_parameters(kwargs)

        # Finds the coefficients
        values, vect = self.Galerkin_expansion_coefficients(parsed_parameters)

        # Selects fastest growing mode
        ok = np.argmax(values.real)
        # Stores the growth rate and expansion coefficients
        self.growth_rate = values[ok]

        self.coefficients = vect[ok].real

        local_r_sph_grid = self.grid.r_spherical.get_local_data()
        local_theta_grid = self.grid.theta.get_local_data()
        local_phi_grid = self.grid.phi.get_local_data()

        local_arrays = [np.zeros_like(local_r_sph_grid) for i in range(3)]

        if not (parsed_parameters['halo_growing_mode_only'] and
                self.growth_rate<0):

            ref_radius = parsed_parameters['halo_ref_radius']


            ref_theta = np.arccos(parsed_parameters['halo_ref_z']/ref_radius)
            ref_radius = np.array([ref_radius])
            ref_theta = np.array([ref_theta])

            halo_radius = parsed_parameters['halo_radius']
            symmetric = parsed_parameters['halo_symmetric_field']

            # Computes the normalization at the solar radius
            Bsun_p = np.array([0.])

            for i, coefficient in enumerate(self.coefficients):
                # Calculates free-decay modes locally
                Bmode = halo_free_decay_modes.get_mode(
                  local_r_sph_grid/halo_radius, local_theta_grid,
                  local_phi_grid, i+1, symmetric)

                for j in range(3):
                    local_arrays[j] += Bmode[j] * coefficient

                Brefs = halo_free_decay_modes.get_mode(
                    ref_radius/halo_radius, ref_theta, np.array([0.]), i+1,
                    symmetric)

                Bsun_p += Brefs[2] * coefficient

            Bnorm = parsed_parameters['halo_ref_Bphi']/Bsun_p[0]

            for i in range(3):
                local_arrays[i] *= Bnorm

        # Initializes global arrays
        global_arrays = \
            [self.grid.get_prototype(dtype=self.dtype) for i in xrange(3)]

        # Bring the local array data into the d2o's
        for (g, l) in zip(global_arrays, local_arrays):
            g.set_local_data(l, copy=False)

        # Prepares the result field
        result_field = B_field_component(grid=self.grid,
                                         r_spherical=global_arrays[0],
                                         theta=global_arrays[1],
                                         phi=global_arrays[2],
                                         dtype=self.dtype,
                                         generator=self,
                                         parameters=parsed_parameters)

        return result_field

    def perturbation_operator(self, r, theta, phi, Br, Bt, Bp, Vr, Vt, Vp,
                              alpha, Ra, Ro, dynamo_type='alpha-omega'):
        """ Applies the perturbation operator associated with an
            a dynamo to a magnetic field in spherical coordinates.

            Input:
                r, B, alpha, V: position vector (not radius!), magnetic field,
                alpha and rotation curve, respectively, expressed as 3xNxNxN arrays
                containing the r, theta and phi components in [0,...], [1,...]
                and [2,...], respectively.
                p: dictionary of parameters containing 'Ralpha_h'.
            Output:
                Returns a 3xNxNxN array containing W(B)
        """
        # Computes \nabla \times (\alpha B)
        curl_aB = curl_spherical(r, theta, phi, Br*alpha, Bt*alpha, Bp*alpha)

        # Computes \nabla \times (V \times B)
        VcrossBr = (Vt*Bp - Vp*Bt)
        VcrossBt = (Vp*Br - Vr*Bp)
        VcrossBp = (Vr*Bt - Vt*Br)

        curl_VcrossB = curl_spherical(r, theta, phi,
                                      VcrossBr, VcrossBt, VcrossBp)
        WBs = []
        for i in range(3):
            if dynamo_type=='alpha-omega':
                WBs.append(Ra*(curl_aB[i] - curl_aB[2])  \
                              + Ro*curl_VcrossB[i])

            elif dynamo_type=='alpha2-omega':
                WBs.append(Ra*curl_aB[i] + Ro*curl_VcrossB[i])

            else:
                raise AssertionError('Invalid option: dynamo_type={0}'.format(dynamo_type))

        return WBs

    def Galerkin_expansion_coefficients(self, parameters,
                                        return_matrix=False):
        """
        Calculates the Galerkin expansion coefficients.

        First computes the transformation M defined by:
        Mij = gamma_j, for i=j
        Mij = Wij, for i!=j
        where:
        W_{ij} = \int B_j \cdot \hat{W} B_i
        Then, solves the eigenvalue/eigenvector problem.

        Input:
            p: dictionary of parameters containing the parameters:
              halo_Galerkin_ngrid -> Number of grid points used in the
                                     calculation of the Galerkin expansion
              halo_rotation_function -> a function specifying the halo rotation
                                        curve
              halo_alpha_function -> a function specifying the dependence of
                                     the alpha effect
              halo_turbulent_induction -> R_{\alpha}
              halo_rotation_induction -> R_{\omega}
              halo_n_free_decay_modes -> number of free decay modes to be used
                                         in the expansion
              halo_symmetric_field -> Symmetric or anti-symmetric field
                                      solution

        Output: (Same as the output of numpy.linalg.eig)
            Gammas: n-array containing growth rates (the eigenvalues of Mij)
            ai's: nx3 array containing the Galerkin coefficients associated
            with each growth rate (the eigenvectors)
        """
        nGalerkin = parameters['halo_Galerkin_ngrid']
        function_V = parameters['halo_rotation_function']
        function_alpha = parameters['halo_alpha_function']
        Ralpha = parameters['halo_turbulent_induction']
        Romega = parameters['halo_rotation_induction']
        nmodes = parameters['halo_n_free_decay_modes']
        symmetric = parameters['halo_symmetric_field']

        # Prepares a spherical grid for the Galerkin expansion
        galerkin_grid = Grid(box=[[0.00001,1.0], # r range
                             [0.01,np.pi],  # theta range
                             [0.0,0.0]], # phi range
                             resolution=[nGalerkin,nGalerkin,1],
                             grid_type='spherical')

        local_r_sph_grid = galerkin_grid.r_spherical.get_local_data()
        local_phi_grid = galerkin_grid.phi.get_local_data()
        local_theta_grid = galerkin_grid.theta.get_local_data()

        # local_B_r_spherical, local_B_phi, local_B_theta (for each mode)
        local_Bmodes = []
        Bmodes = []
        for imode in range(1,nmodes+1):
            # Calculates free-decay modes locally
            local_Bmodes.append(halo_free_decay_modes.get_mode(
                                                            local_r_sph_grid,
                                                            local_theta_grid,
                                                            local_phi_grid,
                                                            imode, symmetric))
            # Initializes global arrays
            Bmodes.append([galerkin_grid.get_prototype(dtype=self.dtype)
                                 for i in xrange(3)])


        for k in range(nmodes):
            # Brings the local array data into the d2o's
            for (g, l) in zip(Bmodes[k], local_Bmodes[k]):
                g.set_local_data(l, copy=False)

        # Computes sintheta
        local_sintheta = np.sin(local_theta_grid)
        # Computes alpha (locally)
        local_alpha = function_alpha(local_r_sph_grid,
                                     local_theta_grid,
                                     local_phi_grid)
        # Computes the various components of V (locally)
        local_Vs = function_V(local_r_sph_grid,
                              local_theta_grid,
                              local_phi_grid)
        # Brings sintheta, rotation curve and alpha into the d2o's
        sintheta = galerkin_grid.get_prototype(dtype=self.dtype)
        sintheta.set_local_data(local_sintheta, copy=False)
        alpha = galerkin_grid.get_prototype(dtype=self.dtype)
        Vs = [galerkin_grid.get_prototype(dtype=self.dtype) for i in xrange(3)]
        alpha.set_local_data(local_alpha, copy=False)
        for (g, l) in zip(Vs, local_Vs):
            g.set_local_data(l, copy=False)

        # Applies the perturbation operator
        WBmodes = []
        for Bmode in Bmodes:
            WBmodes.append(self.perturbation_operator(galerkin_grid.r_spherical,
                                                 galerkin_grid.theta,
                                                 galerkin_grid.phi,
                                                 Bmode[0], Bmode[1], Bmode[2],
                                                 Vs[0], Vs[1], Vs[2], alpha,
                                                 Ralpha, Romega,
                                                 parameters['halo_dynamo_type']
                                                 ))

        Wij = np.zeros((nmodes,nmodes))
        for i in range(nmodes):
            for j in range(nmodes):
                if i==j:
                    continue
                integrand = galerkin_grid.get_prototype(dtype=self.dtype)
                integrand *= 0.0
                for k in range(3):
                    integrand += Bmodes[i][k]*WBmodes[j][k]

                integrand *= galerkin_grid.r_spherical**2 * sintheta

                # Integrates over phi assuming axisymmetry
                integrand = integrand[:,:,0]*2.0*np.pi
                # Integrates over theta
                integrand = simpson(integrand, galerkin_grid.theta[:,:,0])
                # Integrates over r
                Wij[i,j] += simpson(integrand,galerkin_grid.r_spherical[:,0,0])
        # Overwrites the diagonal with its correct (gamma) values
        if symmetric:
            gamma = halo_free_decay_modes.gamma_s
        else:
            gamma = halo_free_decay_modes.gamma_a

        for i in range(nmodes):
            Wij[i,i] = gamma[i]

        # Solves the eigenvector problem and returns the result
        val, vec = np.linalg.eig(Wij)
        if not return_matrix:
            return val, vec
        else:
            return val, vec, Wij


