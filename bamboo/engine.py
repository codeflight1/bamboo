"""
Main Engine class, as well as tools for specifying engine geometry and the perfect gas model used to calculate flow properties.

References:
 - [1] - https://en.wikipedia.org/wiki/Nusselt_number
 - [2] - https://en.wikipedia.org/wiki/Darcy_friction_factor_formulae
 - [3] - https://en.wikipedia.org/wiki/Darcy%E2%80%93Weisbach_equation
 - [4] - Huang and Huzel, Modern Engineering for Design of Liquid-Propellant Rocket Engines

Notes:
 - With some exceptions, fluid properties are currently evaluated at the bulk temperature, instead of the film temperature. This is because the high wall temperature can sometimes 
   be above the fluid boiling point, which can cause errors. Ideally you would use nucleate boiling correlations in this case, but this is not always possible, and so use the bulk 
   properties is available as a compromise.
"""

import numpy as np
import scipy.optimize
import scipy.special
import matplotlib.pyplot as plt
import matplotlib.patches
import warnings
#import time

import bamboo.rao
import bamboo.isen
import bamboo.sim
import bamboo.circuit

# Constants
R_BAR = 8.3144621e3         # Universal gas constant (J/K/kmol)
REDH_LAMINAR = 2300         # Maximum Reynolds number for laminar flow in a pipe

class PerfectGas:
    """Object to store a perfect gas model (i.e. an ideal gas with constant cp and cv). You only need to input 2 properties to fully define it.

    Keyword Args:
        gamma (float): Ratio of specific heats cp/cv.
        cp (float): Specific heat capacity at constant pressure (J/kg/K)
        molecular_weight (float): Molecular weight of the gas (kg/kmol)

    Attributes:
        gamma (float): Ratio of specific heats cp/cv.
        cp (float): Specific heat capacity at constant pressure (J/kg/K)
        molecular_weight (float): Molecular weight of the gas (kg/kmol)
        R (float): Specific gas constant (J/kg/K)
    """
    def __init__(self, **kwargs):
        if len(kwargs) > 2:
            raise ValueError(f"Gas object is overdefined. You mustn't provide more than 2 inputs when creating the Gas object. You provided {len(kwargs)}.")

        elif "gamma" in kwargs and "molecular_weight" in kwargs: 
            self.gamma = kwargs["gamma"]
            self.molecular_weight = kwargs["molecular_weight"]
            self.R = R_BAR/self.molecular_weight  
            self.cp = (self.gamma*self.R)/(self.gamma-1)    

        elif "gamma" in kwargs and "cp" in kwargs: 
            self.gamma = kwargs["gamma"]
            self.cp = kwargs["cp"]
            self.R = self.cp*(self.gamma-1)/self.gamma
            self.molecular_weight = R_BAR/self.R

        else:
            raise ValueError(f"Not enough inputs provided to fully define the PerfectGas, or you used a combination of inputs that isn't currently allowable. You must provide exactly 2 inputs, but you provided {len(kwargs)}.")

    def __repr__(self):
        return f"<nozzle.perfect_gas object> with: \ngamma = {self.gamma} \ncp = {self.cp} \nmolecular_weight = {self.molecular_weight} \nR = {self.R}"

class ChamberConditions:
    """Object for storing combustion chamber thermodynamic conditions. The mass flow rate does not have to be defined - it is fixed by the nozzle throat area.

    Args:
        p0 (float): Gas stagnation pressure (Pa).
        T0 (float): Gas stagnation temperature (K).
    """
    def __init__(self, p0, T0):
        self.p0 = p0
        self.T0 = T0

class Geometry:
    def __init__(self, xs, ys):
        """Class for representing the inner contour of a rocket engine, from the beginning of the combustion chamber to the nozzle exit.

        Args:
            xs (list): Array of x-positions, that the 'y' list corresponds to (m). Must be increasing values of x.
            ys (list): Array, containing the distances from the engine centreline to the inner wall (m).

        Attributes:
            xt (float): x-position of the throat (m)
            Rt (float): Throat radius (m)
            At (float): Throat area (m2)
            Re (float): Exit radius (m)
            Ae (float): Exit area (m2)
        """
        self.xs = xs
        self.ys = ys

    @property
    def xt(self):
        return self.xs[np.argmin(self.ys)]

    @property
    def Rt(self):
        return min(self.ys)

    @property
    def At(self):
        return np.pi * self.Rt**2

    @property
    def Re(self):
        return self.ys[-1]
    
    @property
    def Ae(self):
        return np.pi * self.Re**2

    def plot(self):
        """
        Plot the engine geometry. Must run bamboo.plot.show() or matplotlib.pyplot.show() to see the plot.
        """
        fig, axs = plt.subplots()
        axs.plot(self.xs, self.ys, color = "blue")
        axs.plot(self.xs, -np.array(self.ys), color = "blue")
        axs.grid()
        axs.set_xlabel("x (m)")
        axs.set_ylabel("y (m)")
        axs.set_aspect('equal')


    def y(self, x):
        """Get the distance from the centreline to the inner wall of the engine.

        Args:
            x (float): x position (m)

        Returns:
            float: Distance from engine centreline to edge of inner wall (m)
        """
        return np.interp(x, self.xs, self.ys)

    def A(self, x):
        """Get the flow area for the exhaust gas

        Args:
            x (float): x position (m)

        Returns:
            float: Flow area (m2)
        """
        return np.pi * self.y(x)**2

class Wall:
    def __init__(self, material, thickness):
        """Object for representing an engine wall.

        Args:
            material (Material): Material object to define the material the wall is made of.
            thickness (float or callable): Thickness of the wall (m). Can be a constant float, or a function of position, i.e. t(x).
        """
        self.material = material
        self._thickness = thickness

        assert type(thickness) is float or type(thickness) is int or callable(thickness), "'thickness' input must be a float, int or callable"

    def thickness(self, x):
        """Get the thickness of the wall at a position x.

        Args:
            x (float): Axial position along the engine (m)

        Returns:
            float: Wall thickness (m)
        """
        if callable(self._thickness):
            return self._thickness(x)

        else:
            return self._thickness

class CoolingJacket:
    def __init__(self, T_coolant_in, p0_coolant_in, mdot_coolant, channel_height, coolant_transport, roughness = None, configuration = "vertical", **kwargs):
        """Class for representing cooling jacket properties. 

        Note:
            Spiralling channels are assumed to cover the entire surface area of the outer chamber wall (i.e. the width of the channels is equal to the pitch). A blockage ratio can still be used to 'block up' part of the channels with fins.

        Note:
            Spiralling channels are assumed to have a rectangular cross section.

        Args:
            T_coolant_in (float): Inlet temperature of the coolant (K)
            p0_coolant_in (float): Inlet stagnation pressure of the coolant (Pa)
            mdot_coolant (float): Mass flow rate of the coolant (kg/s)
            channel_height (float or callable): Radial height of the cooling channels (i.e. the distance between the inner and outer wall that the coolant flows through) (m). Can be a constant float, or function of axial position (x).
            coolant_transport (TransportProperties): Transport properties of the coolant
            configuration (str, optional): Type of cooling channel. Either 'vertical' for straight, axial, channels. Or 'spiral' for a helix around the engine. Defaults to "vertical".
            roughness (float or callable, optional): Wall roughness in the channel (m), for pressure drop calculations. Can be a constant float, or function of axial position (x). Defaults to None, in which case a smooth walled approximation is used.
        
        Keyword Args:
            blockage_ratio (float or callable): This is the proportion (by area) of the channel cross section occupied by fins. Can be a constant float, or a function of axial position (x). Defaults to zero.
            number_of_fins (int): Only relevant if 'blockage_ratio' !=0. This is the number of fins present in the cooling channel. For spiral channels this is the number of fins 'per pitch' - it is numerically equal to the number of channels that are spiralling around in parallel.
            pitch (float or callable): Only relevant if configuration = 'spiral'. This is the total width (i.e. pitch) of the cooling channels (m). Can be a constant float, or a function of axial position (x).
            xs (list): Minimum and maximum x value which the cooling jacket is present over, e.g. (x_min, x_max). Can be in either order. (m)
            restrain_fins (bool): Whether or not the fins in cooling channels are physically restrained by (i.e. attached to) the outer cooling jacket. This affects the pressure stress. Automatically ignored if blockage_ratio = 0. Defaults to True.
            """

        # Check that the user has not mispelt or used additional kwargs
        allowed_kwargs = {"blockage_ratio", "number_of_fins", "pitch", "xs", "restrain_fins"}
        left_over = set(kwargs.keys()) - allowed_kwargs
        assert not left_over, f'Unrecognised keyword arguments for CoolingJacket: {left_over}'

        # Main code
        assert configuration == "vertical" or configuration == "spiral", "'configuration' input must be either 'vertical' or 'spiral'"

        self.T_coolant_in = T_coolant_in
        self.p0_coolant_in = p0_coolant_in
        self.mdot_coolant = mdot_coolant
        self.coolant_transport = coolant_transport
        self._channel_height = channel_height
        self._roughness = roughness

        self.configuration = configuration

        if "restrain_fins" in kwargs:
            self.restrain_fins = kwargs["restrain_fins"]
            assert type(self.restrain_fins) is bool, f"'restrain_fins' argument must be True or False. It cannot be of type {type(self.restrain_fins)}"

        else:
            self.restrain_fins = True

        if "xs" in kwargs:
            self.xs = kwargs["xs"]

        if self.configuration == "spiral":
            assert "pitch" in kwargs, "Must input 'pitch' in order to use configuration = 'spiral'"
            self._pitch = kwargs["pitch"]

        if "blockage_ratio" in kwargs:
            self._blockage_ratio = kwargs["blockage_ratio"]

            if "number_of_fins" in kwargs:
                self.number_of_fins = kwargs["number_of_fins"]
                assert type(self.number_of_fins) is int, "Keyword argument 'number_of_fins' must be an integer"

                if self.configuration == "spiral":
                    assert self.number_of_fins >= 1, "Keyword argument 'number_of_fins' must be at least 1 for spiral channels (it is numerically equal to the number of channels in parallel)."

            elif configuration == "spiral":
                self.number_of_fins = 1

            elif configuration == "vertical":
                raise ValueError("Must also specify 'number_of_fins' for configuration = 'vertical', if you want to specify 'blockage_ratio'")

        else:
            self._blockage_ratio = 0
            
            if self.configuration == "spiral":
                self.number_of_fins = 1

            elif self.configuration == "vertical":
                self.number_of_fins = 0

    def channel_height(self, x):
        """Get the channel height at a position, x.

        Args:
            x (float): Axial position along the engine (m)

        Returns:
            float: Channel height (m)
        """
        if callable(self._channel_height):
            return self._channel_height(x)

        else:
            return self._channel_height

    def blockage_ratio(self, x):
        """Get the blockage ratio at a position, x.

        Args:
            x (float): Axial position along the engine (m)

        Returns:
            float: Blockage ratio
        """
        if callable(self._blockage_ratio):
            return self._blockage_ratio(x)

        else:
            return self._blockage_ratio
        
    def pitch(self, x):
        """Get the channel width for a spiral channel, at a position, x.

        Args:
            x (float): Axial position along the engine (m)

        Returns:
            float: Channel width (m)
        """
        if callable(self._pitch):
            return self._pitch(x)

        else:
            return self._pitch

    def roughness(self, x):
        """Get the channel roughness, at a position, x.

        Args:
            x (float): Axial position along the engine (m)

        Returns:
            float: Wall roughness of the channel (m)
        """
        if callable(self._roughness):
            return self._roughness(x)

        else:
            return self._roughness

    def f_darcy(self, ReDh, Dh, x):
        # Check for laminar flow
        if ReDh < REDH_LAMINAR:
            return 64.0 / ReDh      # Reference [3]

        else:
            roughness = self.roughness(x)
            if roughness == None:
                # Putukhov equation [1]
                return (0.79 * np.log(ReDh) - 1.64)**(-2)   

            else:
                # Colebrook-White with Lambert W function [2]
                a = 2.51 / ReDh
                two_a = 2*a
                b = roughness / (3.71 * Dh)
                
                return ( (2 * scipy.special.lambertw(np.log(10) / two_a * 10**(b/two_a) )) / np.log(10) - b/a )**(-2)


class Engine:
    """Class for representing a liquid rocket engine.

    Args:
        perfect_gas (PerfectGas): PerfectGas representing the exhaust gas for the engine.
        chamber_conditions (ChamberConditions): ChamberConditions for the engine.
        geometry (Geometry): Geomtry object to define the engine's contour.
        coolant_convection (str): Convective heat transfer model to use for the coolant side. Can be 'dittus-boelter', 'sieder-tate' or 'gnielinski'. Defaults to 'gnielinski'.
        exhaust_convection (str): Convective heat transfer model to use the for exhaust side. Can be 'dittus-boelter', 'bartz' or 'bartz-sigma'. Defaults to 'bartz-sigma'.

    Keyword Args:
        walls (Wall or list): Either a single Wall object that specifies the combustion chamber wall, or a list of Wall objects that represent multiple layers with different materials. List must be in the order [hottest_wall, ... , coldest_wall].
        cooling_jacket (CoolingJacket): CoolingJacket object to specify the cooling jacket on the engine.
        exhaust_transport (TransportProperties): TransportProperties object that defines the exhaust gas transport properties.

    Attributes:
        mdot (float): Mass flow rate of exhaust gas (kg/s)
        c_star (float): C* for the engine (m/s).
        coolant_convection (str): Convective heat transfer model to use for the coolant side.
        exhaust_convection (str): Convective heat transfer model to use the for exhaust side.
        walls (list): List of Wall objects between the hot gas and coolant

    """
    def __init__(self, perfect_gas, chamber_conditions, geometry, coolant_convection = "gnielinski", exhaust_convection = "bartz-sigma", **kwargs):
        # Check that the user has not mispelt or used additional kwargs
        allowed_kwargs = {"walls", "cooling_jacket", "exhaust_transport"}
        left_over = set(kwargs.keys()) - allowed_kwargs
        assert not left_over, f'Unrecognised keyword arguments for Engine: {left_over}'
        
        # Main code
        self.perfect_gas = perfect_gas
        self.chamber_conditions = chamber_conditions
        self.geometry = geometry
        self.coolant_convection = coolant_convection
        self.exhaust_convection = exhaust_convection

        # Find the choked mass flow rate
        self.mdot = bamboo.isen.get_choked_mdot(self.perfect_gas, self.chamber_conditions, self.geometry.At)

        # C* value, for convenience with 'bartz-sigma' convection model
        self.c_star = self.chamber_conditions.p0 * self.geometry.At / self.mdot

        # Additional keyword arguments
        if "walls" in kwargs:
            # Note that you can represent multiple layers of materials by giving a list as 'walls'
            self.walls = kwargs["walls"]

            # If we got a single wall, turn it into a list of length 1.
            if not (type(self.walls) is list):
                self.walls = [self.walls]

            for item in self.walls:
                assert type(item) is Wall, "All items in the walls list must be a Wall object. Otherwise a single Wall object must be given."
        
        if "cooling_jacket" in kwargs:
            self.cooling_jacket = kwargs["cooling_jacket"]
            assert type(self.cooling_jacket) is CoolingJacket
        
        if "exhaust_transport" in kwargs:
            self.exhaust_transport = kwargs["exhaust_transport"]  

    # Exhaust gas functions
    def M(self, x):
        """Get exhaust gas Mach number.

        Args:
            x (float): Axial position along the engine (m). 

        Returns:
            float: Mach number of the freestream.
        """
        #If we're at the throat then M = 1 by default:
        if abs(x - self.geometry.xt) <= 1e-12:
            return 1.00

        #If we're not at the throat:
        else:
            # Collect the relevant variables
            A = self.geometry.A(x)
            mdot = self.mdot
            p0 = self.chamber_conditions.p0
            cp = self.perfect_gas.cp
            T0 = self.chamber_conditions.T0
            gamma = self.perfect_gas.gamma

            # Function to find the root of
            def func_to_solve(Mach):
                return mdot * (cp * T0)**0.5 / (A  * p0) - bamboo.isen.m_bar(M = Mach, gamma = gamma)
 
            if x > self.geometry.xt:
                Mach = scipy.optimize.root_scalar(func_to_solve, bracket = [1, 500], x0 = 1).root
            else:
                Mach = scipy.optimize.root_scalar(func_to_solve, bracket = [0.0,1], x0 = 0.5).root
            return Mach

    def T(self, x):
        """Get temperature at a position along the nozzle.
        Args:
            x (float): Distance from the throat, along the centreline (m)
        Returns:
            float: Temperature (K)
        """
        return bamboo.isen.T(T0 = self.chamber_conditions.T0, M = self.M(x), gamma = self.perfect_gas.gamma)

    def p(self, x):
        """Get pressure at a position along the nozzle.
        Args:
            x (float): Distance from the throat, along the centreline (m)
        Returns:
            float: Pressure (Pa)
        """
        return bamboo.isen.p(p0 = self.chamber_conditions.p0, M = self.M(x), gamma = self.perfect_gas.gamma)

    def rho(self, x):
        """Get exhaust gas density.
        Args:
            x (float): Axial position. Throat is at x = 0.
        Returns:
            float: Freestream gas density (kg/m3)
        """
    
        return self.p(x) / (self.T(x) * self.perfect_gas.R) # p = rho R T for an ideal gas, so rho = p/RT

    # Geometry functions
    def total_wall_thickness(self, x):
        thickness = 0.0
        for wall in self.walls:
            thickness += wall.thickness(x)
        
        return thickness

    def plot(self):
        """Plot the engine geometry, including the cooling channels, all to scale. You will need to run matplotlib.pyplot.show() or bamboo.plot.show() to see the plot.
        """
        num_grid = 1000                         # Resolution to plot to (i.e. number of points to plot)

        if hasattr(self, "walls"):
            fig, axs = plt.subplots()

            xs = np.linspace(self.geometry.xs[0], self.geometry.xs[-1], num_grid)

            # Plot the walls
            for i in range(len(self.walls)):
                # First wall as the chamber as the inner y value
                if i == 0:
                    y_bottom = np.zeros(len(xs))
                    y_top = np.zeros(len(xs))

                    for j in range(len(y_bottom)):
                        y_bottom[j] = self.geometry.y(xs[j])
                        y_top[j] = y_bottom[j] + self.walls[i].thickness(xs[j])

                else:
                    for j in range(len(y_bottom)):
                        y_top[j] = y_bottom[j] + self.walls[i].thickness(xs[j])

                last_plot = axs.fill_between(xs, y_bottom, y_top, label = f'Wall {i+1} (k = {self.walls[i].material.k:#.3g})')
                axs.fill_between(xs, -y_bottom, -y_top, color = last_plot.get_facecolor())

                y_bottom = y_top.copy()
                
            # Plot the cooling channels
            if hasattr(self.cooling_jacket, "xs"):
                min_x_jacket = min(self.cooling_jacket.xs)
                max_x_jacket = max(self.cooling_jacket.xs)
            else:
                min_x_jacket = self.geometry.xs[0]
                max_x_jacket = self.geometry.xs[-1]

            # Vertical cooling channels
            if self.cooling_jacket.configuration == "vertical":
                for j in range(len(y_bottom)):
                    y_top[j] = y_bottom[j] + self.cooling_jacket.channel_height(xs[j])

                # Cooling channel may only be applied over a specific range
                x_channel = []
                y_bottom_channel = []
                y_top_channel = []

                for k in range(len(xs)):
                    if xs[k] >= min_x_jacket and xs[k] <= max_x_jacket:
                        x_channel.append(xs[k])
                        y_bottom_channel.append(y_bottom[k])
                        y_top_channel.append(y_top[k])

                axs.fill_between(x_channel, y_bottom_channel, y_top_channel, label = f'Cooling channel', color = "blue")
                axs.fill_between(x_channel, -np.array(y_bottom_channel), -np.array(y_top_channel), color = "blue")

            # Spiralling cooling channels - modified from Bamboo 0.1.1
            elif self.cooling_jacket.configuration == "spiral":
                #Just for the legends
                axs.plot(0, 0, color = 'blue', label = 'Cooling channels')  

                if self.cooling_jacket.number_of_fins != 1:

                    axs.plot(0, 0, color = 'red', label = 'Channel fins')  
                    fin_color = 'red'

                else:
                    fin_color = 'blue'

                #Plot the spiral channels as rectangles
                current_x = min_x_jacket

                while current_x < max_x_jacket:
                    y_jacket_inner = np.interp(current_x, xs, y_bottom)
                    H = self.cooling_jacket.channel_height(current_x)           # Current channel height
                    W = self.cooling_jacket.pitch(current_x)            # Current channel width

                    #Show the ribs as filled in rectangles
                    area_per_fin = W * H * self.cooling_jacket.blockage_ratio(current_x)/self.cooling_jacket.number_of_fins
                    fin_width = area_per_fin / H

                    for j in range(self.cooling_jacket.number_of_fins):
                        distance_to_next_rib = W/self.cooling_jacket.number_of_fins

                        # Make all ribs red
                        axs.add_patch(matplotlib.patches.Rectangle([current_x + j*distance_to_next_rib, y_jacket_inner], fin_width, H, color = fin_color, fill = True))
                        axs.add_patch(matplotlib.patches.Rectangle([current_x + j*distance_to_next_rib, -y_jacket_inner-H], fin_width, H, color = fin_color, fill = True))

                    # Plot 'outer' cooling channel (i.e. the amount moved per spiral)
                    axs.add_patch(matplotlib.patches.Rectangle([current_x, y_jacket_inner], W, H, color = 'blue', fill = False))
                    axs.add_patch(matplotlib.patches.Rectangle([current_x, -y_jacket_inner-H], W, H, color = 'blue', fill = False))

                    current_x = current_x + W
            
            axs.grid()
            axs.legend()
            axs.set_aspect('equal')
            axs.set_xlabel("x (m)")
            axs.set_ylabel("y (m)")

            # Reverse the legend order, so they're arranged in the same order as the lines usually are
            handles, labels = axs.get_legend_handles_labels()
            axs.legend(reversed(handles), reversed(labels))
        
        else:
            fig, axs = plt.subplots()

            line = axs.plot(self.geometry.xs, self.geometry.ys)
            axs.plot(self.geometry.xs, -np.array(self.geometry.ys), color = line[0].get_color())
            axs.grid()
            axs.set_aspect('equal')
            axs.set_xlabel("x (m)")
            axs.set_ylabel("y (m)")

    # Cooling jacket functions
    def A_coolant(self, x):
        """Flow area of the coolant at an axial position.

        Args:
            x (float): Axial position x (m)

        Returns:
            float: Coolant flow area (m2)
        """
        if self.cooling_jacket.configuration == "vertical":
            R_in = self.geometry.y(x) + self.total_wall_thickness(x)
            R_out = R_in + self.cooling_jacket.channel_height(x)
            flow_area_unblocked = np.pi * (R_out**2 - R_in**2)
            return flow_area_unblocked * (1 - self.cooling_jacket.blockage_ratio(x))

        elif self.cooling_jacket.configuration == "spiral":
            flow_area_unblocked = self.cooling_jacket.pitch(x) * self.cooling_jacket.channel_height(x)
            return flow_area_unblocked * (1 - self.cooling_jacket.blockage_ratio(x))

    def Dh_coolant(self, x):
        """Hydraulic diameter of the coolant flow channel - used for pressure drops. This is equal to 4 * A / P, where 'A' is the coolant flow area and 'P' is the perimeter of the channel.

        Args:
            x (float): Axial position (m)

        Returns:
            float: Hydraulic diameter (m)
        """
        channel_height = self.cooling_jacket.channel_height(x)

        if self.cooling_jacket.configuration == 'spiral':
            perimeter = 2 * self.cooling_jacket.pitch(x) + 2 * channel_height + 2 * channel_height * self.cooling_jacket.number_of_fins
            return 4 * self.A_coolant(x) / perimeter

        elif self.cooling_jacket.configuration == 'vertical':
            R_in = self.geometry.y(x) + self.total_wall_thickness(x)
            perimeter = (2*np.pi*R_in + 2*np.pi*(R_in + channel_height)) * (1 - self.cooling_jacket.blockage_ratio(x)) + 2 * self.cooling_jacket.number_of_fins * channel_height
            return 4 * self.A_coolant(x) / perimeter

    def V_coolant(self, x, rho_coolant):
        """Get the coolant velocity at an axial position.

        Args:
            x (float): Axial position (m)
            rho_coolant (float): Coolant density (kg/m3)

        Returns:
            float: Coolant velocity (m/s)
        """
        assert hasattr(self, "cooling_jacket"), "Must have given a 'cooling_jacket' input to the Engine object to use Engine.coolant_velocity()"
        return self.cooling_jacket.mdot_coolant / (rho_coolant * self.A_coolant(x))

    def p_coolant(self, x, p0_coolant, rho_coolant):
        """Get the static pressure of the coolant from the stagnation pressure. Uses Bernoulli's equation, which assumes the coolant to be incompressible. 

        Args:
            x (float): Axial position (m)
            p0_coolant (float): Stagnation pressure (Pa)
            rho_coolant (float): Coolant density (kg/m3)

        Returns:
            float: Static pressure (Pa)
        """
        return p0_coolant - 0.5 * rho_coolant * self.V_coolant(x = x, rho_coolant = rho_coolant)**2

    def rho_coolant(self, x, T_coolant, p0_coolant):
        """Use iteration to find the coolant density. It's a function of pressure, but we don't know the static pressure since it is a function of the density (from Bernoulli).

        Args:
            x (float): Axial position (m)
            T_coolant (float): Coolant temperature (K)
            p0_coolant (float): Coolant stagnation pressure

        Returns:
            float: Coolant density (kg/m3)
        """
        
        # Initial guess of density using stagnation pressure
        rho_coolant = self.cooling_jacket.coolant_transport.rho(T = T_coolant, p = p0_coolant)

        # Iterate
        change = np.inf
        while change > rho_coolant * 1e-12:
            p_coolant = self.p_coolant(x = x, p0_coolant = p0_coolant, rho_coolant = rho_coolant)
            new_rho_coolant = self.cooling_jacket.coolant_transport.rho(T = T_coolant, p = p_coolant)
            change = new_rho_coolant - rho_coolant
            rho_coolant = new_rho_coolant

        return rho_coolant

    # Thrust functions
    def thrust(self, p_amb = 1e5):
        """Get the thrust of the engine for a given ambient pressure

        Args:
            p_amb (float, optional): Ambient pressure (Pa). Defaults to 1e5.

        Returns:
            float: Thrust (N)
        """

        Me = self.M(x = self.geometry.xs[-1])
        Te = self.T(x = self.geometry.xs[-1])
        pe = self.p(x = self.geometry.xs[-1])

        return self.mdot * Me * (self.perfect_gas.gamma * self.perfect_gas.R * Te)**0.5 + (pe - p_amb) * self.geometry.Ae    #Generic equation for rocket thrust
    
    def isp(self, p_amb = 1e5):
        """Get the specific impulse of the engine for a given ambient pressure.

        Args:
            p_amb (float, optional): Ambient pressure (Pa). Defaults to 1e5.

        Returns:
            float: Specific impulse (m/s)
        """
        return self.thrust(p_amb = p_amb) / self.mdot

    # Functions that need to be submitted to bamboo.sim.HXSolver
    def T_h(self, state):
        return self.T(state["x"])

    def cp_c(self, state):
        rho_coolant = self.rho_coolant(x = state["x"], T_coolant = state["T_c"], p0_coolant = state["p0_c"])
        p_coolant = self.p_coolant(x = state["x"], p0_coolant = state["p0_c"], rho_coolant = rho_coolant)

        return self.cooling_jacket.coolant_transport.cp(T = state["T_c"], p = p_coolant)

    def R_th(self, state):
        R_list = []

        # Need a list of thermal circuit resistances [R1, R2 ...], in the order T_cold --> T_hot
        x = state["x"]
        y = self.geometry.y(x)
        T_coolant = state["T_c"]
        T_coolant_wall = state["T_cw"]
        p0_coolant = state["p0_c"]

        # COOLANT
        # Collect all the coolant transport properties, and find the convective resistance
        rho_coolant = self.rho_coolant(x = x, T_coolant = T_coolant, p0_coolant = p0_coolant)
        p_coolant = self.p_coolant(x = x, p0_coolant = p0_coolant, rho_coolant = rho_coolant)
        V_coolant = self.V_coolant(x = x, rho_coolant = rho_coolant)
        Dh_coolant = self.Dh_coolant(x = x)
        
        Pr_coolant = self.cooling_jacket.coolant_transport.Pr(T = T_coolant, p = p_coolant)
        mu_coolant = self.cooling_jacket.coolant_transport.mu(T = T_coolant, p = p_coolant)
        k_coolant = self.cooling_jacket.coolant_transport.k(T = T_coolant, p = p_coolant)

        ReDh_coolant = rho_coolant * V_coolant * Dh_coolant / mu_coolant

        if ReDh_coolant < REDH_LAMINAR:
            # Laminar flow
            warnings.warn(f"ReDh < {REDH_LAMINAR} in cooling channels: Laminar flow relations will be used (may cause a step in temperature graphs). Constant wall temperature is assumed for Nusselt number.", stacklevel = 2)
            NuDh_coolant = 3.66       # Nusselt number for constant wall temperature approximation, Reference [1]
            self.h_coolant = NuDh_coolant * k_coolant / Dh_coolant

        elif self.coolant_convection == "dittus-boelter":
            self.h_coolant = bamboo.circuit.h_coolant_dittus_boelter(rho = rho_coolant, 
                                                                V = V_coolant, 
                                                                D = Dh_coolant, 
                                                                mu = mu_coolant, 
                                                                Pr = Pr_coolant, 
                                                                k = k_coolant)

        elif self.coolant_convection == "sieder-tate":
            mu_coolant_wall = self.cooling_jacket.coolant_transport.mu(T = T_coolant_wall, p = p_coolant)
            self.h_coolant = bamboo.circuit.h_coolant_sieder_tate(rho = rho_coolant, 
                                                                V = V_coolant, 
                                                                D = Dh_coolant, 
                                                                mu_bulk = mu_coolant, 
                                                                mu_wall = mu_coolant_wall,
                                                                Pr = Pr_coolant, 
                                                                k = k_coolant)

        elif self.coolant_convection == "gnielinski":
            f_darcy = self.cooling_jacket.f_darcy(Dh = Dh_coolant, ReDh = ReDh_coolant, x = x)
            self.h_coolant = bamboo.circuit.h_coolant_gnielinski(rho = rho_coolant, 
                                                                 V = V_coolant, 
                                                                 D = Dh_coolant, 
                                                                 mu = mu_coolant, 
                                                                 Pr = Pr_coolant, 
                                                                 k = k_coolant,
                                                                 f_darcy = f_darcy)

        else:
            raise ValueError(f"Coolant convection model '{self.coolant_convection}' is not recognised. Try 'gnielinski', 'sieder-tate', or 'dittus-boelter'")

        A_coolant = 2 * np.pi * (y + self.total_wall_thickness(x) + self.cooling_jacket.channel_height(x))      # Note, this is the area per unit axial length. We will multiply by 'dx' later in the bamboo.sim.HXSolver
        R_list.append(1.0 / (self.h_coolant * A_coolant))
        
        # SOLID WALLS
        # Find the thermal resistance of the solid boundaries between the coolant and the gas - note our resistance list goes in the order [Cold --> Hot], but the walls are in the order [Hot --> Cold]
        for i in range(len(self.walls)):   
            # Work in reverse from the cold side to the hot side
            reversed_walls = list(reversed(self.walls))

            # Calculate the inner radius - need to add up all the wall thickness up to (and excluding) the current wall
            r1 = y
            for j in range(len(self.walls) - i - 1):
                r1 += self.walls[j].thickness(x)

            r2 = r1 + reversed_walls[i].thickness(x)

            R_list.append(np.log(r2/r1) / (2 * np.pi * reversed_walls[i].material.k))

        # EXHAUST GAS
        # Get the gas properties, and find the thermal resistance of the convection on the hot gas side
        rho_exhaust = self.rho(x)
        T_exhaust = self.T(x)
        T_exhaust_wall = state["T_hw"]
        p_exhaust = self.p(x)
        M_exhaust = self.M(x)
        V_exhaust = (self.perfect_gas.gamma * self.perfect_gas.R * T_exhaust)**0.5 * M_exhaust      # V = sqrt(gamma * R * T) * M, from speed of sound for an ideal gas
        Dh_exhaust = 2 * y
        mu_exhaust = self.exhaust_transport.mu(T = T_exhaust, p = p_exhaust)
        Pr_exhaust = self.exhaust_transport.Pr(T = T_exhaust, p = p_exhaust)
        k_exhaust = self.exhaust_transport.k(T = T_exhaust, p = p_exhaust)


        if self.exhaust_convection == "dittus-boelter":
            h_exhaust = bamboo.circuit.h_coolant_dittus_boelter(rho = rho_exhaust, 
                                                                V = V_exhaust, 
                                                                D = Dh_exhaust, 
                                                                mu = mu_exhaust, 
                                                                Pr = Pr_exhaust, 
                                                                k = k_exhaust)

        elif self.exhaust_convection == "bartz":
            T_exhaust_am = (T_exhaust + T_exhaust_wall) / 2                                                             # Arithmetic mean of wall and freestream
            mu_exhaust_am = self.exhaust_transport.mu(T = T_exhaust_am, p = p_exhaust)
            rho_exhaust_am = p_exhaust/(self.perfect_gas.R * T_exhaust_am)
            mu_exhaust_0 = self.exhaust_transport.mu(T = self.chamber_conditions.T0, p = self.chamber_conditions.p0)    # At stagnation conditions

            h_exhaust = bamboo.circuit.h_gas_bartz(D = Dh_exhaust, 
                                                   cp_inf = self.perfect_gas.cp, 
                                                   mu_inf = mu_exhaust, 
                                                   Pr_inf = Pr_exhaust, 
                                                   rho_inf = rho_exhaust, 
                                                   v_inf = V_exhaust, 
                                                   rho_am = rho_exhaust_am, 
                                                   mu_am = mu_exhaust_am,
                                                   mu0 = mu_exhaust_0)

        elif self.exhaust_convection == "bartz-sigma":
            mu_exhaust_0 = self.exhaust_transport.mu(T = self.chamber_conditions.T0, p = self.chamber_conditions.p0)    # At stagnation conditions
            Pr_exhaust_0 = self.exhaust_transport.Pr(T = self.chamber_conditions.T0, p = self.chamber_conditions.p0)   
            
            h_exhaust = bamboo.circuit.h_gas_bartz_sigma(c_star = self.c_star, 
                                                         At = self.geometry.At, 
                                                         A = np.pi * Dh_exhaust**2 / 4, 
                                                         p_chamber = self.chamber_conditions.p0, 
                                                         T_chamber = self.chamber_conditions.T0, 
                                                         M = M_exhaust, 
                                                         Tw = T_exhaust_wall, 
                                                         mu0 = mu_exhaust_0, 
                                                         cp0 = self.perfect_gas.cp, 
                                                         gamma = self.perfect_gas.gamma, 
                                                         Pr0 = Pr_exhaust_0)

        A_exhaust = 2 * np.pi * y                       # Note, this is the area per unit axial length. We will multiply by 'dx' later in the bamboo.sim.HXSolver
        R_list.append(1.0 / (h_exhaust * A_exhaust))
        
        return R_list

    def extra_dQ_dx(self, state):
        x = state["x"]
        blockage_ratio = self.cooling_jacket.blockage_ratio(x)

        if blockage_ratio == 0 or abs(blockage_ratio) < 1e-12: 
            return 0.0      # Effectively no fins

        else:
            P = 2.0                                     # For dQ, the perimeter is 2 * dx. We must divide this by dx to get dQ/dx
            L = self.cooling_jacket.channel_height(x)
            
            R = self.geometry.y(x)
            for i in range(len(self.walls)):
                R += self.walls[i].thickness(x)

            if self.cooling_jacket.configuration == "vertical":
                # Base area (per dx) for a single fin = circumference * blockage_ratio / number_of_fins. Assume the fins are constant cross sectional area, equal to the base area.
                Ac = 2 * np.pi * R * blockage_ratio / self.cooling_jacket.number_of_fins  

            elif self.cooling_jacket.configuration == "spiral":
                pitch = self.cooling_jacket.pitch(x)
                Ac = pitch * blockage_ratio / self.cooling_jacket.number_of_fins

            T_b = state["T_cw"]
            T_inf = state["T_c"]

            # extra_dQ_dx should always be called after R_th was called, so we can reuse the convective heat transfer coefficients calculacted from it.
            dQ_dx_single_fin = bamboo.circuit.Q_fin_adiabatic(P = P, 
                                                              Ac = Ac, 
                                                              k = self.walls[-1].material.k, 
                                                              h = self.h_coolant, 
                                                              L = L, 
                                                              T_b = T_b, 
                                                              T_inf = T_inf)
            
            if self.cooling_jacket.configuration == "vertical":
                # Subtract the heat transfer that we added assuming there were no fins, and replace it with the heat transfer from the fins
                return abs(dQ_dx_single_fin * self.cooling_jacket.number_of_fins) - 2 * np.pi * R * (1 - blockage_ratio) * self.h_coolant * (T_b - T_inf)   

            elif self.cooling_jacket.configuration == "spiral":
                return abs(dQ_dx_single_fin * self.cooling_jacket.number_of_fins) - pitch * (1 - blockage_ratio) * self.h_coolant * (T_b - T_inf)   

    def dp_dx(self, state):
        x = state["x"]
        T_coolant = state["T_c"]
        p0_coolant = state["p0_c"]
        
        Dh = self.Dh_coolant(x)
        rho_coolant = self.rho_coolant(x = x, T_coolant = T_coolant, p0_coolant = p0_coolant)
        p_coolant = self.p_coolant(x = x, p0_coolant = p0_coolant, rho_coolant = rho_coolant)
        V_coolant = self.V_coolant(x = x, rho_coolant = rho_coolant)
        mu_coolant = self.cooling_jacket.coolant_transport.mu(T = T_coolant, p = p_coolant)
        
        ReDh = rho_coolant * V_coolant * Dh / mu_coolant

        f_darcy = self.cooling_jacket.f_darcy(Dh = Dh, ReDh = ReDh, x = x)

        # Fully developed pipe flow pressure drop [3] - this is dp/dL (pressure drop per unit length travelled by the fluid)
        dp_dL = f_darcy * (rho_coolant / 2) * (V_coolant**2)/Dh

        # For vertical channels, dp/dL = dp/dx
        if self.cooling_jacket.configuration == "vertical":
            return dp_dL
        
        # Need to add a scale factor for the fact that 'dx' is not the same as the path length that the fluid takes around the spiral
        if self.cooling_jacket.configuration == "spiral":
            pitch = self.cooling_jacket.pitch(x)
            
            R = self.geometry.y(x)
            for i in range(len(self.walls)):
                R += self.walls[i].thickness(x)

            circumference = 2 * np.pi * R
            helix_angle = np.arctan(circumference / pitch)
            dL_dx = 1 / np.cos(helix_angle)                 # Length travelled along the spiral for each 'dx' you move axially

            return dp_dL * dL_dx

    # Functions for thermal simulations
    def steady_heating_analysis(self, num_grid = 1000, counterflow = True, iter_start = 5, iter_each = 1):
        """Run a steady state cooling simulation.

        Args:
            num_grid (int): Number of grid points to use (1-dimensional)
            counterflow (bool, optional): Whether or not the cooling is flowing coutnerflow or coflow, relative to the exhaust gas. Defaults to True (which means counterflow).
            iter_start (int): Number of times to iterate on the entry conditions.
            iter_each (int): Number of times to iterate on the solution at each datapoint.
        """

        dx = (self.geometry.xs[0] - self.geometry.xs[-1]) / num_grid

        # Check that we have all the required inputs.
        assert hasattr(self, "cooling_jacket"), "'cooling_jacket' input must be given to Engine object in order to run a steady cooling simulation"
        assert hasattr(self, "exhaust_transport"), "'exhaust_transport' input must be given to Engine object in order to run a steady cooling simulation"
        assert hasattr(self, "walls"), "'walls' input must be given to Engine object in order to run a cooling simulation"

        if hasattr(self.cooling_jacket, "xs"):
            x_min = min(self.cooling_jacket.xs)

            assert x_min >= self.geometry.xs[0], f"The 'xs' input to your CoolingJacket implies that the cooling jacket starts before the beginning of the engine (i.e. upstream of the injector end, x = {self.geometry.xs[0]})."

            x_max = max(self.cooling_jacket.xs)

            assert x_max <= self.geometry.xs[-1], f"The 'xs' input to your CoolingJacket implies that the cooling jacket goes beyond the end of the engine (i.e. beyond the nozzle end, x = {self.geometry.xs[-1]})"
        
        else:
            x_max = self.geometry.xs[-1]
            x_min = self.geometry.xs[0]

        if counterflow:
            dx = -abs(dx)
            x_start = x_max
            x_end = x_min

        else:
            dx = abs(dx)
            x_start = x_min
            x_end = x_max

        # Set up and run simulation
        cooling_simulation = bamboo.sim.HXSolver(T_c_in = self.cooling_jacket.T_coolant_in, 
                                                          T_h = self.T_h, 
                                                          p0_c_in = self.cooling_jacket.p0_coolant_in, 
                                                          cp_c = self.cp_c, 
                                                          mdot_c = self.cooling_jacket.mdot_coolant, 
                                                          R_th = self.R_th,  
                                                          extra_dQ_dx = self.extra_dQ_dx,
                                                          dp_dx = self.dp_dx, 
                                                          x_start = x_start, 
                                                          dx = dx, 
                                                          x_end = x_end)

        cooling_simulation.run(iter_start = iter_start, iter_each = iter_each)

        # Run through the results, and convert them into a convenient form, as well as calculating any useful-to-know values
        if len(self.walls) > 1:
            warnings.warn("More than one wall is present. Thermal stresses calculations will ignore any incompatibility in different thermal expansions.", stacklevel = 2)

        results = {}
        results["info"] = {}                                                            
        results["x"]                    = [None] * len(cooling_simulation.state)       
        results["T"]                    = [None] * len(cooling_simulation.state)     
        results["T_coolant"]            = None
        results["T_exhaust"]            = None
        results["dQ_dx"]                = [None] * len(cooling_simulation.state)        
        results["dQ_dA"]                = [None] * len(cooling_simulation.state)       
        results["p0_coolant"]           = [None] * len(cooling_simulation.state)     
        results["p_coolant"]            = [None] * len(cooling_simulation.state)           
        results["rho_coolant"]          = [None] * len(cooling_simulation.state)        
        results["V_coolant"]            = [None] * len(cooling_simulation.state)        
        results["sigma_t_thermal"]      = [None] * len(cooling_simulation.state)        
        results["sigma_t_pressure"]     = [None] * len(cooling_simulation.state)        
        results["sigma_t_max"]          = [None] * len(cooling_simulation.state)        

        # Explanation of what all the keys mean
        results["info"]["x"] = "Axial position along the engine (m)."
        results["info"]["T"] = "Temperature at each position (K). T[i][j], is the temperature at x[i], at the j'th wall boundary. j = 0 corresponds to the coolant, j = -1 corresponds to the exhaust gas."
        results["info"]["T_coolant"] = "Coolant temperature at each position (K). T_coolant[i] is the value at x[i]."
        results["info"]["T_exhaust"] = "Exhaust temperature at each position (K). T_exhaust[i] is the value at x[i]. "
        results["info"]["dQ_dx"] = "Heat transfer rate per unit axial length (W/m). dQ_dx[i] is the value at x[i]."
        results["info"]["dQ_dA"] = "Heat transfer rate per unit chamber area (W/m2). dQ_dA[i] is the value at x[i]."
        results["info"]["p0_coolant"] = "Stagnation pressure of coolant (Pa). p0_coolant[i] is the value at x[i]."
        results["info"]["rho_coolant"] = "Density of coolant (kg/m3). rho_coolant[i] is the value at x[i]."
        results["info"]["p_coolant"] = "Static pressure of coolant (Pa). p_coolant[i] is the value at x[i]."
        results["info"]["V_coolant"] = "Velocity of coolant (m/s). V_coolant[i] is the value at x[i]."
        results["info"]["sigma_t_thermal"] = "Tangential stress due to uneven thermal expansion (Pa). sigma_t_thermal[i][j] corresponds to the stress at x[i], across the j'th wall. j = 0 is the wall in contact with the exhaust gas, j = -1 is the wall in contact with the coolant."
        results["info"]["sigma_t_pressure"] = "Tangential stress due to pressure difference across wall (Pa). sigma_t_pressure[i][j] corresponds to the stress at x[i], across the j'th wall. j = 0 is the wall in contact with the exhaust gas, j = -1 is the wall in contact with the coolant."
        results["info"]["sigma_t_max"] = "Maximum tangential stress (Pa), equal to abs(sigma_t_thermal) + abs(sigma_t_pressure). sigma_t_max[i][j] corresponds to the stress at x[i], across the j'th wall. j = 0 is the wall in contact with the exhaust gas, j = -1 is the wall in contact with the coolant."

        for i in range(len(cooling_simulation.state)):
            x = cooling_simulation.state[i]["x"]

            # Collect all the data into a dictionary
            results["x"][i] = x
            results["T"][i] = list(cooling_simulation.state[i]["circuit"].T)
            results["dQ_dx"][i] = -cooling_simulation.state[i]["circuit"].Qdot
            results["dQ_dA"][i] = results["dQ_dx"][i] / (2 * np.pi * self.geometry.y(x = results["x"][i]))
            results["p0_coolant"][i] = cooling_simulation.state[i]["p0_c"]
            results["rho_coolant"][i] = self.rho_coolant(x = results["x"][i], T_coolant = cooling_simulation.state[i]["T_c"], p0_coolant = results["p0_coolant"][i])
            results["p_coolant"][i] = self.p_coolant(x = results["x"][i], p0_coolant = results["p0_coolant"][i], rho_coolant = results["rho_coolant"][i])
            results["V_coolant"][i] = self.V_coolant(x = results["x"][i], rho_coolant = results["rho_coolant"][i])

            # Calculate relevant stresses
            results["sigma_t_thermal"][i] = [None] * len(self.walls)
            results["sigma_t_pressure"][i] = [None] * len(self.walls)
            results["sigma_t_max"][i] = [None] * len(self.walls)
            
            # Calculate these now to avoid them being recalculated unnecessarily
            p_l = results["p_coolant"][i]                           # Coolant pressure (Pa)
            p_g = self.p(results["x"][i])                           # Exhaust pressure (Pa)
            blockage_ratio = self.cooling_jacket.blockage_ratio(x)  # Channel blockage ratio
            D = 2 * self.geometry.y(x)                              # Engine diameter (up to relevant wall) (m)
            t_w = 0                                                 # Wall thickness (will be updated as we go) (m)

            if self.cooling_jacket.configuration == "spiral":
                pitch = self.cooling_jacket.pitch(x)

            # Iterate through each wall
            for j in range(len(self.walls)):
                D += t_w

                # Thermal stress from Huzel and Huang [4]
                E = self.walls[j].material.E
                alpha = self.walls[j].material.alpha
                k = self.walls[j].material.k
                poisson = self.walls[j].material.poisson
                t_w = self.walls[j].thickness(x)

                results["sigma_t_thermal"][i][j] = E * alpha * results["dQ_dA"][i] * t_w / (2 * (1 - poisson) * k)

                # Pressure stress from Huzel and Huang [4]
                D += t_w / 2        # Average diameter

                # If we don't have fins in the cooling channels
                if abs(blockage_ratio) < 1e-12 or self.cooling_jacket.restrain_fins == False:
                    results["sigma_t_pressure"][i][j] = (p_l - p_g) * D / (2 * t_w)

                # If we have fins in the cooling channels, and the fins restrain the inner wall (by being attached to the outer jacket)
                else:
                    if self.cooling_jacket.configuration == "vertical":
                        w = np.pi * D * (1 - blockage_ratio) / self.cooling_jacket.number_of_fins

                    elif self.cooling_jacket.configuration == "spiral":
                        w = pitch * (1 - blockage_ratio)
                    
                    results["sigma_t_pressure"][i][j] = 0.5 * (p_l - p_g) * (w / t_w)**2

                # Total stress from Huzel and Huang [4]
                results["sigma_t_max"][i][j] = abs(results["sigma_t_thermal"][i][j]) + abs(results["sigma_t_pressure"][i][j])

                # Remove t_w / 2, so the next wall calculation uses the right diameter
                D -= t_w / 2

        results["T_exhaust"] = list(np.array(results["T"])[:, -1])
        results["T_coolant"] = list(np.array(results["T"])[:, 0])

        return results