# -*- coding: utf-8 -*-
# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but without any warranty; without even the implied warranty of
#  merchantability or fitness for a particular purpose.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# ##### END GPL LICENSE BLOCK #####

#### TODO:
# sanitize the constructors so that they don't edit their parent object
# apply island rotation and position before exporting, to simplify things
# rename verts -> vertices, edge.vect -> edge.vector
# SVG object doesn't need a 'pure_net' argument in constructor
# maybe Island would do with a list of points as well, set of vertices makes things more complicated
# why does UVVertex copy its position in constructor?
# is it necessary to keep island.boundary sorted? Could save some time
# remember selected objects before baking, except selected to active
# islands with default names should be excluded while matching
# add 'estimated number of pages' to the export UI

# check conflicts in island naming and either:
#  * append a number to the conflicting names or
#  * enumerate faces uniquely within all islands of the same name (requires a check that both label and abbr. equals)


bl_info = {
	"name": "Export Paper Model",
	"author": "Addam Dominec",
	"version": (0, 9),
	"blender": (2, 70, 0),
	"location": "File > Export > Paper Model",
	"warning": "",
	"description": "Export printable net of the active mesh",
	"category": "Import-Export",
	"wiki_url": "http://wiki.blender.org/index.php/Extensions:2.6/Py/"
		"Scripts/Import-Export/Paper_Model",
	"tracker_url": "https://projects.blender.org/tracker/index.php?"
		"func=detail&aid=22417&group_id=153&atid=467"
}

"""

Additional links:
	e-mail: adominec {at} gmail {dot} com

"""
import bpy
import bgl
import mathutils as M
from re import compile as re_compile
from itertools import chain
from math import pi

try:
	import os.path as os_path
except ImportError:
	os_path = None

try:
	from blist import blist
except ImportError:
	blist = list

default_priority_effect = {
	'CONVEX': 0.5,
	'CONCAVE': 1,
	'LENGTH': -0.05
}


def first_letters(text):
	"""Iterator over the first letter of each word"""
	for match in first_letters.pattern.finditer(text):
		yield text[match.start()]
first_letters.pattern = re_compile("(?<!\w)[\w]")


def pairs(sequence):
	"""Generate consecutive pairs throughout the given sequence; at last, it gives elements last, first."""
	i = iter(sequence)
	previous = first = next(i)
	for this in i:
		yield previous, this
		previous = this
	yield this, first


def argmax_pair(array, key):
	"""Find an (unordered) pair of indices that maximize the given function"""
	l = len(array)
	mi, mj, m = None, None, None
	for i in range(l):
		for j in range(i+1, l):
			k = key(array[i], array[j])
			if not m or k > m:
				mi, mj, m = i, j, k
	return mi, mj


def fitting_matrix(v1, v2):
	"""Get a matrix that rotates v1 to the same direction as v2"""
	return (1 / v1.length_squared) * M.Matrix((
		(v1.x*v2.x + v1.y*v2.y, v1.y*v2.x - v1.x*v2.y),
		(v1.x*v2.y - v1.y*v2.x, v1.x*v2.x + v1.y*v2.y)))


def z_up_matrix(n):
	"""Get a rotation matrix that aligns given vector upwards."""
	b = n.xy.length
	l = n.length
	if b > 0:
		return M.Matrix((
			(n.x*n.z/(b*l), n.y*n.z/(b*l), -b/l),
			(-n.y/b, n.x/b, 0),
			(0, 0, 0)
		))
	else:
		# no need for rotation
		return M.Matrix((
			(1, 0, 0),
			(0, (-1 if n.z < 0 else 1), 0),
			(0, 0, 0)
		))


def create_blank_image(image_name, dimensions, alpha=1):
	"""Create a new image and assign white color to all its pixels"""
	image_name = image_name[:64]
	width, height = int(dimensions.x), int(dimensions.y)
	image = bpy.data.images.new(image_name, width, height, alpha=True)
	if image.users > 0:
		raise UnfoldError("There is something wrong with the material of the model. "
			"Please report this on the BlenderArtists forum. Export failed.")
	image.pixels = [1, 1, 1, alpha] * (width * height)
	image.file_format = 'PNG'
	return image


class UnfoldError(ValueError):
	pass


class Unfolder:
	def __init__(self, ob):
		self.ob = ob
		self.mesh = Mesh(ob.data, ob.matrix_world)
		self.tex = None

	def prepare(self, page_size=None, create_uvmap=False, mark_seams=False, priority_effect=default_priority_effect, scale=1):
		"""Create the islands of the net"""
		page_size = page_size / scale if page_size else None
		self.mesh.generate_cuts(page_size, priority_effect)
		self.mesh.finalize_islands(scale_factor=scale, aspect_ratio = 210/297)
		self.mesh.enumerate_islands()
		if create_uvmap:
			self.tex = self.mesh.save_uv()
		if mark_seams:
			self.mesh.mark_cuts()
	
	def copy_island_names(self, island_list):
		"""Copy island label and abbreviation from the best matching island in the list"""
		orig_list = {(frozenset(face.id for face in item.faces), item.label, item.abbreviation) for item in island_list}
		for island in self.mesh.islands:
			islfaces = {uvface.face.index for uvface in island.faces}
			match = max(orig_list, key=lambda item: islfaces.intersection(item[0]))
			island.label = match[1]
			island.abbreviation = match[2]
	
	def save(self, properties):
		"""Export the document"""
		# Note about scale: input is direcly in blender length.
		# finalize_islands multiplies everything by scale/page_size.y
		# SVG object multiplies everything by page_size.y*ppm.
		filepath = properties.filepath
		if filepath.lower().endswith((".svg", ".png")):
			filepath = filepath[0:-4]
		# page size in meters
		page_size = M.Vector((properties.output_size_x, properties.output_size_y))
		# printable area size in meters
		printable_size = page_size - 2 * properties.output_margin * M.Vector((1, 1))
		scale = bpy.context.scene.unit_settings.scale_length / properties.scale
		ppm = properties.output_dpi * 100 / 2.54  # pixels per meter
		
		if properties.do_create_stickers:
			self.mesh.generate_stickers(properties.sticker_width * printable_size.y / scale, properties.do_create_numbers)
		elif properties.do_create_numbers:
			self.mesh.generate_numbers_alone(properties.sticker_width * printable_size.y / scale)
			
		text_height = 12 / (printable_size.y*ppm) if (properties.do_create_numbers and len(self.mesh.islands) > 1) else 0
		aspect_ratio = printable_size.x / printable_size.y
		# finalizing islands will scale everything so that the page height is 1
		self.mesh.finalize_islands(scale_factor=scale/printable_size.y, title_height=text_height, aspect_ratio=aspect_ratio)
		self.mesh.fit_islands(aspect_ratio=aspect_ratio)
		
		if properties.output_type != 'NONE':
			# bake an image and save it as a PNG to disk or into memory
			use_separate_images = properties.image_packing in ('ISLAND_LINK', 'ISLAND_EMBED')
			tex = self.mesh.save_uv(aspect_ratio=printable_size.x/printable_size.y, separate_image=use_separate_images, tex=self.tex)
			if not tex:
				raise UnfoldError("The mesh has no UV Map slots left. Either delete a UV Map or export the net without textures.")
			if properties.output_type == 'TEXTURE' and tex.active_render:
				tex.active = True
				bpy.ops.mesh.uv_texture_remove()
				raise UnfoldError("Texture bake error. Probably a UV Map is missing.")
			rd = bpy.context.scene.render
			recall = rd.bake_type, rd.use_bake_to_vertex_color, rd.use_bake_selected_to_active, rd.bake_distance, rd.bake_bias, rd.bake_margin, rd.use_bake_clear
			
			if properties.output_type == 'RENDER':
				rd.bake_type = 'FULL'
				rd.use_bake_selected_to_active = False
			
			elif properties.output_type == 'TEXTURE':
				rd.bake_type = 'TEXTURE'
				rd.use_bake_selected_to_active = False
				recall_materials = [slot.material for slot in self.ob.material_slots]
				mat = bpy.data.materials.new("unfolder_temp")
				mat.use_shadeless = True
				mat.use_face_texture = True
				for slot in self.ob.material_slots:
					slot.material = mat
				if not recall_materials:
					# this is a crazy hack. Sometimes, slots must be accessed
					# through ob.material_slots, sometimes ob.data.materials
					self.ob.data.materials.append(mat)
			
			elif properties.output_type == 'SELECTED_TO_ACTIVE':
				rd.bake_type = 'FULL'
				rd.use_bake_selected_to_active = True
			
			rd.bake_margin, rd.bake_distance, rd.bake_bias, rd.use_bake_to_vertex_color, rd.use_bake_clear = 0, 0, 0.001, False, False
			if properties.image_packing == 'PAGE_LINK':
				self.mesh.save_image(tex, printable_size * ppm, filepath)
			elif properties.image_packing == 'ISLAND_LINK':
				self.mesh.save_separate_images(tex, printable_size.y * ppm, filepath)
			elif properties.image_packing == 'ISLAND_EMBED':
				self.mesh.save_separate_images(tex, printable_size.y * ppm, filepath, do_embed=True)
			
			# revoke settings
			rd.bake_type, rd.use_bake_to_vertex_color, rd.use_bake_selected_to_active, rd.bake_distance, rd.bake_bias, rd.bake_margin, rd.use_bake_clear = recall
			if properties.output_type == 'TEXTURE':
				for slot, material in zip(self.ob.material_slots, recall_materials):
					slot.material = material
				if not recall_materials:
					# another crazy workaround, remove the material slot we have created
					self.ob.data.materials.pop()
				mat.user_clear()
				bpy.data.materials.remove(mat)
			if not properties.do_create_uvmap:
				tex.active = True
				bpy.ops.mesh.uv_texture_remove()

		svg = SVG(page_size * ppm, printable_size.y * ppm, properties.style, (properties.output_type == 'NONE'))
		svg.do_create_stickers = properties.do_create_stickers
		svg.margin = properties.output_margin * ppm
		svg.write(self.mesh, filepath)


class Mesh:
	"""Wrapper for Bpy Mesh"""
	
	def __init__(self, mesh, matrix):
		self.verts = dict()
		self.edges = dict()
		self.edges_by_verts_indices = dict()
		self.faces = dict()
		self.islands = list()
		self.data = mesh
		self.pages = list()
		for bpy_vertex in mesh.vertices:
			self.verts[bpy_vertex.index] = Vertex(bpy_vertex, self, matrix)
		for bpy_edge in mesh.edges:
			edge = Edge(bpy_edge, self, matrix)
			self.edges[bpy_edge.index] = edge
			self.edges_by_verts_indices[(edge.va.index, edge.vb.index)] = edge
			self.edges_by_verts_indices[(edge.vb.index, edge.va.index)] = edge
		for bpy_face in mesh.polygons:
			face = Face(bpy_face, self)
			self.faces[bpy_face.index] = face
		for index in self.edges:
			edge = self.edges[index]
			edge.choose_main_faces()
			if edge.main_faces:
				edge.calculate_angle()
	
	def generate_cuts(self, page_size, priority_effect):
		"""Cut the mesh so that it can be unfolded to a flat net."""
		# warning: this constructor modifies its parameter (face)
		islands = {Island(face) for face in self.faces.values()}
		# check for edges that are cut permanently
		edges = [edge for edge in self.edges.values() if not edge.force_cut and len(edge.faces) > 1]
		
		if edges:
			average_length = sum(edge.length for edge in edges) / len(edges)
			for edge in edges:
				edge.generate_priority(priority_effect, average_length)
			edges.sort(reverse=False, key=lambda edge: edge.priority)
			for edge in edges:
				if edge.length == 0:
					continue
				face_a, face_b = edge.main_faces
				island_a, island_b = face_a.uvface.island, face_b.uvface.island
				if len(island_b.faces) > len(island_a.faces):
					island_a, island_b = island_b, island_a
				if island_a is not island_b:
					if island_a.join(island_b, edge, size_limit=page_size):
						islands.remove(island_b)
		
		self.islands = sorted(islands, reverse=True, key=lambda island: len(island.faces))
		
		for edge in self.edges.values():
			# some edges did not know until now whether their angle is convex or concave
			if edge.main_faces and (edge.main_faces[0].uvface.flipped or edge.main_faces[1].uvface.flipped):
				edge.calculate_angle()
			# ensure that the order of faces corresponds to the order of uvedges
			if len(edge.uvedges) >= 2:
				reordered = [None, None]
				for uvedge in edge.uvedges:
					try:
						index = edge.main_faces.index(uvedge.uvface.face)
						reordered[index] = uvedge
					except ValueError:
						reordered.append(uvedge)
				edge.uvedges = reordered

		for island in self.islands:
			# if the normals are ambiguous, flip them so that there are more convex edges than concave ones
			if any(uvface.flipped for uvface in island.faces):
				island_edges = {uvedge.edge for uvedge in island.edges if not uvedge.edge.is_cut(uvedge.uvface.face)}
				balance = sum((+1 if edge.angle > 0 else -1) for edge in island_edges)
				if balance < 0:
					island.is_inside_out = True
		
			# construct a linked list from each island's boundary
			# uvedge.neighbor_right is clockwise = forward = via uvedge.vb if not uvface.flipped
			neighbor_lookup, conflicts = dict(), dict()
			for uvedge in island.boundary:
				uvvertex = uvedge.va if uvedge.uvface.flipped else uvedge.vb
				if uvvertex not in neighbor_lookup:
					neighbor_lookup[uvvertex] = uvedge
				else:
					if uvvertex not in conflicts:
						conflicts[uvvertex] = [neighbor_lookup[uvvertex], uvedge]
					else:
						conflicts[uvvertex].append(uvedge)
			
			for uvedge in island.boundary:
				uvvertex = uvedge.vb if uvedge.uvface.flipped else uvedge.va
				if uvvertex not in conflicts:
					uvedge.neighbor_right = neighbor_lookup[uvvertex]
					uvedge.neighbor_right.neighbor_left = uvedge
				else:
					conflicts[uvvertex].append(uvedge)
			
			# resolve merged vertices with more boundaries crossing
			def direction_to_float(vector):
				return (1 - vector.x/vector.length) if vector.y > 0 else (vector.x/vector.length - 1)
			for uvvertex, uvedges in conflicts.items():
				def is_inwards(uvedge):
					return uvedge.uvface.flipped == (uvedge.va is uvvertex)
				
				def uvedge_sortkey(uvedge):
					if is_inwards(uvedge):
						return direction_to_float(uvedge.va.co - uvedge.vb.co)
					else:
						return direction_to_float(uvedge.vb.co - uvedge.va.co)
				
				uvedges.sort(key=uvedge_sortkey)
				for right, left in zip(uvedges[:-1:2], uvedges[1::2]) if is_inwards(uvedges[0]) else zip([uvedges[-1]] + uvedges[1::2], uvedges[:-1:2]):
					left.neighbor_right = right
					right.neighbor_left = left
		return True
	
	def mark_cuts(self):
		"""Mark cut edges in the original mesh so that the user can see"""
		for bpy_edge in self.data.edges:
			edge = self.edges[bpy_edge.index]
			bpy_edge.use_seam = len(edge.uvedges) > 1 and edge.is_main_cut
	
	def generate_stickers(self, default_width, do_create_numbers=True):
		"""Add sticker faces where they are needed."""
		def uvedge_priority(uvedge):
			"""Retuns whether it is a good idea to stick something on this edge's face"""
			# TODO: it should take into account overlaps with faces and with other stickers
			return uvedge.uvface.face.area / sum((vb.co - va.co).length for (va, vb) in pairs(uvedge.uvface.verts))
		
		for edge in self.edges.values():
			if edge.is_main_cut and len(edge.uvedges) >= 2:
				uvedge_a, uvedge_b = edge.uvedges[:2]
				if uvedge_priority(uvedge_a) < uvedge_priority(uvedge_b):
					uvedge_a, uvedge_b = uvedge_b, uvedge_a
				target_island = uvedge_a.island
				left_edge, right_edge = uvedge_a.neighbor_left.edge, uvedge_a.neighbor_right.edge
				if do_create_numbers:
					for uvedge in [uvedge_b] + edge.uvedges[2:]:
						if ((uvedge.neighbor_left.edge is not right_edge or uvedge.neighbor_right.edge is not left_edge) and
								uvedge not in (uvedge_a.neighbor_left, uvedge_a.neighbor_right)):
							# it will not be clear to see that these uvedges should be sticked together
							# So, create an arrow and put the index on all stickers
							target_island.sticker_numbering += 1
							index = str(target_island.sticker_numbering)
							# if the index would have a different meaning upside down, append a dot
							if {'6', '9'} < set(index) < {'6', '8', '9', '0'}:
								index += "."
							target_island.add_marker(Arrow(uvedge_a, default_width, index))
							break
					else:
						# if all uvedges to be sticked are easy to see, create no numbers
						index = None
				else:
					index = None
				uvedge_b.island.add_marker(Sticker(uvedge_b, default_width, index, target_island))
			elif len(edge.uvedges) > 2:
				index = None
				target_island = edge.uvedges[0].island
			if len(edge.uvedges) > 2:
				for uvedge in edge.uvedges[2:]:
					uvedge.island.add_marker(Sticker(uvedge, default_width, index, target_island))
	
	def generate_numbers_alone(self, size):
		global_numbering = 0
		for edge in self.edges.values():
			if edge.is_main_cut and len(edge.uvedges) >= 2:
				global_numbering += 1
				index = str(global_numbering)
				if ('6' in index or '9' in index) and set(index) <= {'6', '8', '9', '0'}:
					# if index consists of the digits 6, 8, 9, 0 only and contains 6 or 9, make it distinguishable
					index += "."
				for uvedge in edge.uvedges:
					uvedge.island.add_marker(NumberAlone(uvedge, index, size))
	
	def enumerate_islands(self):
		for num, island in enumerate(self.islands, 1):
			island.number = num
			island.generate_label()
	
	def finalize_islands(self, scale_factor=1, title_height=0, aspect_ratio=1):
		for island in self.islands:
			island.apply_scale(scale_factor)
			if title_height:
				island.title = "[{}] {}".format(island.abbreviation, island.label)
			island.generate_bounding_box(space_at_bottom=title_height, aspect_ratio=aspect_ratio)

	def largest_island_ratio(self, page_size):
		return max(max(island.bounding_box.x / page_size.x, island.bounding_box.y / page_size.y) for island in self.islands)
	
	def fit_islands(self, aspect_ratio):
		"""Move islands so that they fit onto pages, based on their bounding boxes"""
		
		def try_emplace(island, page_islands, page_size, stops_x, stops_y, occupied_cache):
			"""Tries to put island to each pair from stops_x, stops_y
			and checks if it overlaps with any islands present on the page.
			Returns True and positions the given island on success."""
			bbox_x, bbox_y = island.bounding_box.xy
			for x in stops_x:
				if x + bbox_x > page_size.x:
					continue
				for y in stops_y:
					if y + bbox_y > page_size.y or (x, y) in occupied_cache:
						continue
					for i, obstacle in enumerate(page_islands):
						# if this obstacle overlaps with the island, try another stop
						if (x + bbox_x > obstacle.pos.x and
								obstacle.pos.x + obstacle.bounding_box.x > x and
								y + bbox_y > obstacle.pos.y and
								obstacle.pos.y + obstacle.bounding_box.y > y):
							if x >= obstacle.pos.x and y >= obstacle.pos.y:
								occupied_cache.add((x, y))
							# just a stupid heuristic to make subsequent searches faster
							if i > 0:
								page_islands[1:i+1] = page_islands[:i]
								page_islands[0] = obstacle
							break
					else:
						# if no obstacle called break, this position is okay
						island.pos.xy = x, y
						page_islands.append(island)
						stops_x.append(x + bbox_x)
						stops_y.append(y + bbox_y)
						return True
			return False
		
		def drop_portion(stops, border, divisor):
			stops.sort()
			# distance from left neighbor to the right one, excluding the first stop
			distances = [right - left for left, right in zip(stops, chain(stops[2:], [border]))]
			quantile = sorted(distances)[len(distances) // divisor]
			return [stop for stop, distance in zip(stops, chain([quantile], distances)) if distance >= quantile]
		
		page_size = M.Vector((aspect_ratio, 1))
		if any(island.bounding_box.x > page_size.x or island.bounding_box.y > page_size.y for island in self.islands):
			raise UnfoldError("An island is too big to fit onto page of the given size. "
				"Either downscale the model or find and split that island manually.\n"
				"Export failed, sorry.")
		# sort islands by their diagonal... just a guess
		remaining_islands = sorted(self.islands, reverse=True, key=lambda island: island.bounding_box.length_squared)
		page_num = 1
		
		while remaining_islands:
			# create a new page and try to fit as many islands onto it as possible
			page = Page(page_num)
			page_num += 1
			occupied_cache = set()
			stops_x, stops_y = [0], [0]
			for island in remaining_islands:
				try_emplace(island, page.islands, page_size, stops_x, stops_y, occupied_cache)
				# if overwhelmed with stops, drop a quarter of them
				if len(stops_x)**2 > 4 * len(self.islands) + 100:
					stops_x = drop_portion(stops_x, page_size.x, 4)
					stops_y = drop_portion(stops_y, page_size.y, 4)
			remaining_islands = [island for island in remaining_islands if island not in page.islands]
			self.pages.append(page)
	
	def save_uv(self, aspect_ratio=1, separate_image=False, tex=None):
		# TODO: mode switching should be handled by higher-level code
		bpy.ops.object.mode_set()
		# note: assuming that the active object's data is self.mesh
		if not tex:
			tex = self.data.uv_textures.new()
			if not tex:
				return None
		tex.name = "Unfolded"
		tex.active = True
		# TODO: this is somewhat dirty, but I do not see a nicer way in the API
		loop = self.data.uv_layers[self.data.uv_layers.active_index]
		if separate_image:
			for island in self.islands:
				island.save_uv_separate(loop)
		else:
			for island in self.islands:
				island.save_uv(loop, aspect_ratio)
		return tex
	
	def save_image(self, tex, page_size_pixels: M.Vector, filename):
		texfaces = tex.data
		# omitting this causes a "Circular reference in texture stack" error
		for island in self.islands:
			for uvface in island.faces:
				texfaces[uvface.face.index].image = None
		
		for page in self.pages:
			image = create_blank_image("{} {} Unfolded".format(self.data.name[:14], page.name), page_size_pixels, alpha=1)
			image.filepath_raw = page.image_path = "{}_{}.png".format(filename, page.name)
			for island in page.islands:
				for uvface in island.faces:
					texfaces[uvface.face.index].image = image
			try:
				bpy.ops.object.bake_image()
				image.save()
			finally:
				for island in page.islands:
					for uvface in island.faces:
						texfaces[uvface.face.index].image = None
				image.user_clear()
				bpy.data.images.remove(image)
	
	def save_separate_images(self, tex, scale, filepath, do_embed=False):
		assert(os_path)  # check the module was imported
		if do_embed:
			import tempfile
			import base64
		else:
			from os import mkdir
			image_dir = filepath
			try:
				mkdir(image_dir)
			except OSError:
				# image_dir already existed
				pass
		
		texfaces = tex.data
		# omitting these 3 lines causes a "Circular reference in texture stack" error
		for island in self.islands:
			for uvface in island.faces:
				texfaces[uvface.face.index].image = None
		
		for i, island in enumerate(self.islands, 1):
			if do_embed:
				tempfile_manager = tempfile.NamedTemporaryFile("rb", suffix=".png")
				image_path = tempfile_manager.name
				image_name = os_path.basename(tempfile_manager.name)
				# note: image_path exists by now and Blender will overwrite it;
				# we will read later from the same file
			else:
				image_path = os_path.join(image_dir, "island{}.png".format(i))
				image_name = "{} isl{}".format(self.data.name[:15], i)
			image = create_blank_image(image_name, island.bounding_box * scale, alpha=0)
			image.filepath_raw = image_path
			for uvface in island.faces:
				texfaces[uvface.face.index].image = image
			
			try:
				bpy.ops.object.bake_image()
				image.save()
			finally:
				for uvface in island.faces:
					texfaces[uvface.face.index].image = None
				image.user_clear()
				bpy.data.images.remove(image)
			
			if do_embed:
				with tempfile_manager as imgfile:
					island.embedded_image = base64.encodebytes(imgfile.read()).decode('ascii')
			else:
				island.image_path = image_path


class Vertex:
	"""BPy Vertex wrapper"""
	__slots__ = ('index', 'co', 'edges', 'uvs')

	def __init__(self, bpy_vertex, mesh=None, matrix=1):
		self.index = bpy_vertex.index
		self.co = matrix * bpy_vertex.co
		self.edges = list()
		self.uvs = list()
	
	def __hash__(self):
		return hash(self.index)
	
	def __eq__(self, other):
		return self.index == other.index


class Edge:
	"""Wrapper for BPy Edge"""
	__slots__ = ('va', 'vb', 'faces', 'main_faces', 'uvedges',
		'vect', 'length', 'angle',
		'is_main_cut', 'force_cut', 'priority')

	def __init__(self, edge, mesh, matrix=1):
		self.va = mesh.verts[edge.vertices[0]]
		self.vb = mesh.verts[edge.vertices[1]]
		self.vect = self.vb.co - self.va.co
		self.length = self.vect.length
		self.faces = list()
		# if self.main_faces is set, then self.uvedges[:2] must be the same to faces as self.main_faces
		# this constraint is assured at the time of finishing mesh.generate_cuts
		self.uvedges = list()
		
		self.force_cut = bool(edge.use_seam)  # such edges will always be cut
		self.main_faces = None  # two faces that may be connected in the island
		# is_main_cut defines whether the two main faces are connected
		# all the others will be assumed to be cut
		self.is_main_cut = True
		self.priority = None
		self.angle = None
		self.va.edges.append(self)
		self.vb.edges.append(self)
	
	def choose_main_faces(self):
		"""Choose two main faces that might get connected in an island"""
		if len(self.faces) == 2:
			self.main_faces = self.faces
		elif len(self.faces) > 2:
			# find (with brute force) the pair of indices whose faces have the most similar normals
			i, j = argmax_pair(self.faces, key=lambda a, b: a.normal.dot(b.normal))
			self.main_faces = self.faces[i], self.faces[j]
	
	def calculate_angle(self):
		"""Calculate the angle between the main faces"""
		face_a, face_b = self.main_faces
		if face_a.normal.length_squared == 0 or face_b.normal.length_squared == 0:
			self.angle = -3 # just a very sharp angle
			return
		# correction if normals are flipped
		a_is_clockwise = ((face_a.verts.index(self.va) - face_a.verts.index(self.vb)) % len(face_a.verts) == 1)
		b_is_clockwise = ((face_b.verts.index(self.va) - face_b.verts.index(self.vb)) % len(face_b.verts) == 1)
		is_equal_flip = True
		if face_a.uvface and face_b.uvface:
			a_is_clockwise ^= face_a.uvface.flipped
			b_is_clockwise ^= face_b.uvface.flipped
			is_equal_flip = (face_a.uvface.flipped == face_b.uvface.flipped)
			assert(a_is_clockwise != b_is_clockwise)
		if a_is_clockwise != b_is_clockwise:
			if (a_is_clockwise == (face_b.normal.cross(face_a.normal).dot(self.vect) > 0)) == is_equal_flip:
				# the angle is convex
				self.angle = face_a.normal.angle(face_b.normal)
			else:
				# the angle is concave
				self.angle = -face_a.normal.angle(face_b.normal)
		else:
			# normals are flipped, so we know nothing
			# so let us assume the angle be convex
			self.angle = face_a.normal.angle(-face_b.normal)

	def generate_priority(self, priority_effect, average_length):
		"""Calculate the priority value for cutting"""
		angle = self.angle
		if angle > 0:
			self.priority = priority_effect['CONVEX'] * angle / pi
		else:
			self.priority = priority_effect['CONCAVE'] * (-angle) / pi
		self.priority += (self.length / average_length) * priority_effect['LENGTH']
	
	def is_cut(self, face):
		"""Return False if this edge will the given face to another one in the resulting net
		(useful for edges with more than two faces connected)"""
		# Return whether there is a cut between the two main faces
		if self.main_faces and face in self.main_faces:
			return self.is_main_cut
		# All other faces (third and more) are automatically treated as cut
		else:
			return True
	
	def other_uvedge(self, this):
		"""Get an uvedge of this edge that is not the given one
		causes an IndexError if case of less than two adjacent edges"""
		return self.uvedges[1] if this is self.uvedges[0] else self.uvedges[0]


class Face:
	"""Wrapper for BPy Face"""
	__slots__ = ('index', 'edges', 'verts', 'uvface',
		'loop_start', 'area', 'normal')

	def __init__(self, bpy_face, mesh, matrix=1):
		self.index = bpy_face.index
		self.edges = list()
		self.verts = [mesh.verts[i] for i in bpy_face.vertices]
		self.loop_start = bpy_face.loop_start
		self.area = bpy_face.area
		self.uvface = None
		
		# calculate the face normal explicitly
		if len(self.verts) == 3:
			# normal of a triangle can be calculated directly
			self.normal = (self.verts[1].co - self.verts[0].co).cross(self.verts[2].co - self.verts[0].co).normalized()
		else:
			# Newell's method
			nor = M.Vector((0, 0, 0))
			for a, b in pairs(self.verts):
				p, m = a.co + b.co, a.co - b.co
				nor.x, nor.y, nor.z = nor.x + m.y*p.z, nor.y + m.z*p.x, nor.z + m.x*p.y
			self.normal = nor.normalized()
		
		for verts_indices in bpy_face.edge_keys:
			edge = mesh.edges_by_verts_indices[verts_indices]
			self.edges.append(edge)
			edge.faces.append(self)
	
	def is_twisted(self):
		if len(self.verts) > 3:
			center = sum(vertex.co for vertex in self.verts) / len(self.verts)
			plane_d = center.dot(self.normal)
			diameter = max((center - vertex.co).length for vertex in self.verts)
			for vertex in self.verts:
				# check coplanarity
				if abs(vertex.co.dot(self.normal) - plane_d) > diameter * 0.01:
					return True
		return False
	
	def __hash__(self):
		return hash(self.index)


class Island:
	"""Part of the net to be exported"""
	__slots__ = ('faces', 'edges', 'verts', 'fake_verts',
		'uvverts_by_id', 'boundary',
		'pos', 'offset', 'angle', 'is_placed', 'bounding_box',
		'image_path', 'embedded_image',
		'number', 'label', 'abbreviation', 'title', 'is_inside_out',
		'scale', 'markers', 'sticker_numbering')
	
	def __init__(self, face=None):
		"""Create an Island from a single Face"""
		self.faces = list()
		self.edges = set()
		self.verts = set()
		self.fake_verts = list()
		self.pos = M.Vector((0, 0))
		self.offset = M.Vector((0, 0))
		self.angle = 0
		self.is_placed = False
		self.bounding_box = M.Vector((0, 0))

		self.image_path = None
		self.embedded_image = None
		
		self.label = None
		self.abbreviation = None
		self.title = None
		self.is_inside_out = False  # swaps concave <-> convex edges
		
		if face:
			uvface = UVFace(face, self)
			self.verts.update(uvface.verts)
			self.faces.append(uvface)
		
		# speedup for Island.join
		self.uvverts_by_id = {uvvertex.vertex.index: [uvvertex] for uvvertex in self.verts}
		# UVEdges on the boundary, sorted left to right
		self.boundary = list(self.edges)
		
		self.scale = 1
		self.markers = list()
		self.sticker_numbering = 0
		self.label = None

	def join(self, other, edge: Edge, size_limit=None, epsilon=1e-6) -> bool:
		"""
		Try to join other island on given edge
		Returns False if they would overlap
		"""
		
		class Intersection(Exception):
			pass
			
		def is_below(self: UVEdge, other: UVEdge):
			if self is other:
				return False
			if self.top < other.bottom:
				return True
			if other.top < self.bottom:
				return False
			if self.max.tup <= other.min.tup:
				return True
			if other.max.tup <= self.min.tup:
				return False
			self_vector = self.max.co - self.min.co
			min_to_min = other.min.co - self.min.co
			cross_b1 = self_vector.cross(min_to_min)
			cross_b2 = self_vector.cross(other.max.co - self.min.co)
			if cross_b1 != 0 or cross_b2 != 0:
				if cross_b1 >= 0 and cross_b2 >= 0:
					return True
				if cross_b1 <= 0 and cross_b2 <= 0:
					return False
			other_vector = other.max.co - other.min.co
			cross_a1 = other_vector.cross(-min_to_min)
			cross_a2 = other_vector.cross(self.max.co - other.min.co)
			if cross_a1 != 0 or cross_a2 != 0:
				if cross_a1 <= 0 and cross_a2 <= 0:
					return True
				if cross_a1 >= 0 and cross_a2 >= 0:
					return False
			if cross_a1 == cross_b1 == cross_a2 == cross_b2 == 0:
				# an especially ugly special case -- lines lying on top of each other
				# Try to resolve instead of throwing an intersection:
				return self.min.tup < other.min.tup or (self.min.tup == other.min.tup and
					self.max.tup < other.max.tup)
			raise Intersection()

		class Sweepline:
			def __init__(self):
				self.children = blist()
			
			def add(self, item, cmp=is_below):
				low, high = 0, len(self.children)
				while low < high:
					mid = (low + high) // 2
					if cmp(self.children[mid], item):
						low = mid + 1
					else:
						high = mid
				# check for intersections
				if low > 0:
					in_order = cmp(self.children[low-1], item)
					assert in_order
				if low < len(self.children):
					in_order = not cmp(self.children[low], item)
					assert in_order
				self.children.insert(low, item)
			
			def remove(self, item, cmp=is_below):
				index = self.children.index(item)
				self.children.pop(index)
				if index > 0 and index < len(self.children):
					# check for intersection
					in_order = not cmp(self.children[index], self.children[index-1])
					assert in_order
		
		def root_find(value, tree):
			"""Find the root of a given value in a forest-like dictionary
			also updates the dictionary using path compression"""
			parent, relink = tree.get(value), list()
			while parent is not None:
				relink.append(value)
				value, parent = parent, tree.get(parent)
			tree.update(dict.fromkeys(relink, value))
			return value

		# find edge in other and in self
		for uvedge in edge.uvedges:
			if uvedge in self.edges:
				uvedge_a = uvedge
			elif uvedge in other.edges:
				uvedge_b = uvedge
		
		# check if vertices and normals are aligned correctly
		verts_flipped = uvedge_b.va.vertex is uvedge_a.va.vertex
		flipped = verts_flipped ^ uvedge_a.uvface.flipped ^ uvedge_b.uvface.flipped
		# determine rotation
		# NOTE: if the edges differ in length, the matrix will involve uniform scaling.
		# Such situation may occur in the case of twisted n-gons
		first_b, second_b = (uvedge_b.va, uvedge_b.vb) if not verts_flipped else (uvedge_b.vb, uvedge_b.va)
		if not flipped:
			rot = fitting_matrix(first_b.co - second_b.co, uvedge_a.vb.co - uvedge_a.va.co)
		else:
			flip = M.Matrix(((-1, 0), (0, 1)))
			rot = fitting_matrix(flip * (first_b.co - second_b.co), uvedge_a.vb.co - uvedge_a.va.co) * flip
		trans = uvedge_a.vb.co - rot * first_b.co
		# extract and transform island_b's boundary
		phantoms = {uvvertex: UVVertex(rot*uvvertex.co + trans, uvvertex.vertex) for uvvertex in other.verts}
		
		# check the size of the resulting island
		if size_limit:
			# first check: bounding box
			bbox_width = max(max(seg.max.co.x for seg in self.boundary), max(vertex.co.x for vertex in phantoms)) - min(min(seg.min.co.x for seg in self.boundary), min(vertex.co.x for vertex in phantoms))
			bbox_height = max(max(seg.top for seg in self.boundary), max(vertex.co.y for vertex in phantoms)) - min(min(seg.bottom for seg in self.boundary), min(vertex.co.y for vertex in phantoms))
			if min(bbox_width, bbox_height)**2 > size_limit.x**2 + size_limit.y**2:
				return False
			if (bbox_width > size_limit.x or bbox_height > size_limit.y) and (bbox_height > size_limit.x or bbox_width > size_limit.y):
				# further checks (TODO!)
				# for the time being, just throw this piece away
				return False
		
		assert edge.vect.length > 0
		distance_limit = edge.vect.length * epsilon
		# try and merge UVVertices closer than sqrt(distance_limit)
		empty = tuple()
		merged_uvedges = set()
		
		# merge all uvvertices that are close enough using a union-find structure
		# uvvertices will be merged only in cases other->self and self->self
		# all resulting groups are merged together to a uvvertex of self
		is_merged_mine = False
		shared_vertices = self.uvverts_by_id.keys() & other.uvverts_by_id.keys()
		for vertex_id in shared_vertices:
			uvs = self.uvverts_by_id[vertex_id] + other.uvverts_by_id[vertex_id]
			len_mine = len(self.uvverts_by_id[vertex_id])
			merged = dict()
			for i, a in enumerate(uvs[:len_mine]):
				i = root_find(i, merged)
				for j, b in enumerate(uvs[i+1:], i+1):
					b = b if j < len_mine else phantoms[b]
					j = root_find(j, merged)
					if i == j:
						continue
					i, j = (j, i) if j < i else (i, j)
					if (a.co - b.co).length_squared < distance_limit:
						merged[j] = i
			for source, target in merged.items():
				target = root_find(target, merged)
				phantoms[uvs[source]] = uvs[target]
				is_merged_mine |= (source < len_mine)  # remember that a vertex of this island has been merged
		
		for uvedge in (chain(self.boundary, other.boundary) if is_merged_mine else other.boundary):
			for partner in uvedge.edge.uvedges:
				if partner is not uvedge:
					# TODO: make sure that this code is okay
					paired_a, paired_b = phantoms.get(partner.vb, partner.vb), phantoms.get(partner.va, partner.va)
					if (partner.uvface.flipped ^ flipped) != uvedge.uvface.flipped:
						paired_a, paired_b = paired_b, paired_a
					if phantoms.get(uvedge.va, uvedge.va) is paired_a and phantoms.get(uvedge.vb, uvedge.vb) is paired_b:
						merged_uvedges.update((uvedge, partner))
						break
		
		if uvedge_b not in merged_uvedges:
			raise UnfoldError("Export failed. Please report this error, including the model if you can.")
		
		boundary_other = [UVEdge(phantoms[uvedge.va], phantoms[uvedge.vb], self)
			for uvedge in other.boundary if uvedge not in merged_uvedges]
		# TODO: if is_merged_mine, it might make sense to create a similar list from self.boundary sorted as well
		
		# check for self-intersections: create event list
		sweepline = Sweepline()
		events_add = [uvedge for uvedge in chain(boundary_other, self.boundary)]
		events_remove = list(events_add)
		events_add.sort(reverse=True, key=lambda uvedge: uvedge.min.tup)
		events_remove.sort(reverse=True, key=lambda uvedge: uvedge.max.tup)
		try:
			while events_remove:
				while events_add and events_add[-1].min.tup <= events_remove[-1].max.tup:
					sweepline.add(events_add.pop())
				sweepline.remove(events_remove.pop())
		except Intersection:
			return False
		
		# mark all edges that connect the islands as not cut
		for uvedge in merged_uvedges:
			uvedge.edge.is_main_cut = False
		
		# include all trasformed vertices as mine
		self.verts.update(phantoms.values())
		
		# update the uvverts_by_id dictionary
		for source, target in phantoms.items():
			present = self.uvverts_by_id.get(target.vertex.index)
			if not present:
				self.uvverts_by_id[target.vertex.index] = [target]
			else:
				# emulation of set behavior... sorry, it is faster
				if source in present:
					present.remove(source)
				if target not in present:
					present.append(target)
		
		# re-link uvedges and uvfaces to their transformed locations
		for uvedge in other.edges:
			uvedge.island = self
			uvedge.va = phantoms[uvedge.va]
			uvedge.vb = phantoms[uvedge.vb]
			uvedge.update()
		if is_merged_mine:
			for uvedge in self.edges:
				uvedge.va = phantoms.get(uvedge.va, uvedge.va)
				uvedge.vb = phantoms.get(uvedge.vb, uvedge.vb)
		self.edges.update(other.edges)
		
		for uvface in other.faces:
			uvface.island = self
			uvface.verts = [phantoms[uvvertex] for uvvertex in uvface.verts]
			uvface.uvvertex_by_id = {index: phantoms[uvvertex]
				for index, uvvertex in uvface.uvvertex_by_id.items()}
			uvface.flipped ^= flipped
		if is_merged_mine:
			# there may be own uvvertices that need to be replaced by phantoms
			for uvface in self.faces:
				if any(uvvertex in phantoms for uvvertex in uvface.verts):
					uvface.verts = [phantoms.get(uvvertex, uvvertex) for uvvertex in uvface.verts]
					uvface.uvvertex_by_id = {index: phantoms.get(uvvertex, uvvertex)
						for index, uvvertex in uvface.uvvertex_by_id.items()}
		self.faces.extend(other.faces)
		
		self.boundary = [uvedge for uvedge in
			chain(self.boundary, other.boundary) if uvedge not in merged_uvedges]
		
		# everything seems to be OK
		return True

	def add_marker(self, marker):
		self.fake_verts.extend(marker.bounds)
		self.markers.append(marker)
	
	def convex_hull(self) -> list:
		"""Returns a list of Vectors that forms the best fitting convex polygon."""
		def make_convex_curve(points):
			"""Remove points from given vert list so that the result poly is a convex curve (works for both top and bottom)."""
			result = list()
			for point in points:
				while len(result) >= 2 and (point - result[-1]).cross(result[-1] - result[-2]) >= 0:
					result.pop()
				result.append(point)
			return result
		points = list(self.fake_verts)
		points.extend(vertex.co for vertex in self.verts)
		points.sort(key=lambda point: point.x)
		points_top = make_convex_curve(points)
		points_bottom = make_convex_curve(reversed(points))
		# remove left and right ends and concatenate the lists to form a polygon in the correct order
		return points_top[:-1] + points_bottom[:-1]
	
	def generate_bounding_box(self, space_at_bottom=0, aspect_ratio=1):
		"""Calculate the rotation for a quite good bounding box"""
		def bounding_box_score(size):
			"""Calculate the score - the bigger result, the better box."""
			return 1/max(size.x, size.y * aspect_ratio)
		points_convex = self.convex_hull()
		if not points_convex:
			raise UnfoldError("Topology error. Try to remove doubled vertices and faces.")
		# go through all edges and search for the best solution
		best_score = 0
		best_box = (0, M.Vector((0, 0)), M.Vector((0, 0)))  # (angle, box, offset) for the best score
		direction_x = M.Vector((1, 0))
		for point_a, point_b in pairs(points_convex):
			angle = direction_x.angle_signed(point_b - point_a, None)
			if angle is None:
				continue
			
			rot = M.Matrix.Rotation(angle, 2)
			# find the dimensions in both directions
			rotated = [rot * point for point in points_convex]
			bottom_left = M.Vector((min(v.x for v in rotated), min(v.y for v in rotated)))
			top_right = M.Vector((max(v.x for v in rotated), max(v.y for v in rotated)))
			box = top_right - bottom_left
			score = bounding_box_score(box)
			if score > best_score:
				best_box = angle, box, bottom_left
				best_score = score
		angle, box, offset = best_box
		box.y += space_at_bottom
		offset.y -= space_at_bottom
		self.angle = angle
		self.bounding_box = box
		self.offset = -offset
	
	def apply_scale(self, scale):
		"""Multiply all coordinates belonging to this Island by the given scale"""
		if scale != 1:
			self.scale *= scale
			for vertex in self.verts:
				vertex.co *= scale
			for point in self.fake_verts:
				point *= scale
	
	def generate_label(self, label=None, abbreviation=None):
		"""Assign a name to this island automatically"""
		abbr = abbreviation or self.abbreviation or str(self.number)
		# TODO: dots should be added in the last instant when outputting any text
		if not set('69NZMWpbqd').isdisjoint(abbr) and set('6890oOxXNZMWIlpbqd').issuperset(abbr):
			abbr += "."
		self.label = label or self.label or "Island {}".format(self.number)
		self.abbreviation = abbr
	
	def save_uv(self, tex, aspect_ratio=1):
		"""Save UV Coordinates of all UVFaces to a given UV texture
		tex: UV Texture layer to use (BPy MeshUVLoopLayer struct)
		page_size: size of the page in pixels (vector)"""
		texface = tex.data
		for uvface in self.faces:
			rot = M.Matrix.Rotation(self.angle, 2)
			for i, uvvertex in enumerate(uvface.verts):
				uv = rot * uvvertex.co + self.offset + self.pos
				texface[uvface.face.loop_start + i].uv[0] = uv.x / aspect_ratio
				texface[uvface.face.loop_start + i].uv[1] = uv.y
	
	def save_uv_separate(self, tex):
		"""Save UV Coordinates of all UVFaces to a given UV texture, spanning from 0 to 1
		tex: UV Texture layer to use (BPy MeshUVLoopLayer struct)
		page_size: size of the page in pixels (vector)"""
		texface = tex.data
		scale_x, scale_y = 1 / self.bounding_box.x, 1 / self.bounding_box.y
		for uvface in self.faces:
			rot = M.Matrix.Rotation(self.angle, 2)
			for i, uvvertex in enumerate(uvface.verts):
				uv = rot * uvvertex.co + self.offset
				texface[uvface.face.loop_start + i].uv[0] = uv.x * scale_x
				texface[uvface.face.loop_start + i].uv[1] = uv.y * scale_y


class Page:
	"""Container for several Islands"""
	__slots__ = ('islands', 'name', 'image_path')

	def __init__(self, num=1):
		self.islands = list()
		self.name = "page{}".format(num)
		self.image_path = None


class UVVertex:
	"""Vertex in 2D"""
	__slots__ = ('co', 'vertex', 'tup')

	def __init__(self, vector, vertex=None):
		if isinstance(vector, UVVertex):
			# Copy constructor
			self.co = vector.co.copy()
			self.vertex = vector.vertex
		else:
			self.co = vector.xy
			self.vertex = vertex
		self.tup = tuple(self.co)
	
	def __str__(self):
		if self.vertex:
			return "UV {} [{:.3f}, {:.3f}]".format(self.vertex.index, self.co.x, self.co.y)
		else:
			return "UV * [{:.3f}, {:.3f}]".format(self.co.x, self.co.y)
	
	__repr__ = __str__


class UVEdge:
	"""Edge in 2D"""
	# Every UVEdge is attached to only one UVFace
	# UVEdges are doubled as needed because they both have to point clockwise around their faces
	__slots__ = ('va', 'vb', 'island', 'uvface', 'edge',
		'min', 'max', 'bottom', 'top',
		'neighbor_left', 'neighbor_right')

	def __init__(self, vertex1: UVVertex, vertex2: UVVertex, island: Island, uvface=None, edge=None):
		self.va = vertex1
		self.vb = vertex2
		self.update()
		self.island = island
		self.uvface = uvface
		if edge:
			self.edge = edge
			edge.uvedges.append(self)
	
	def update(self):
		"""Update data if UVVertices have moved"""
		self.min, self.max = (self.va, self.vb) if (self.va.tup < self.vb.tup) else (self.vb, self.va)
		y1, y2 = self.va.co.y, self.vb.co.y
		self.bottom, self.top = (y1, y2) if y1 < y2 else (y2, y1)
	
	def __str__(self):
		return "({} - {})".format(self.va, self.vb)
	
	__repr__ = __str__


class UVFace:
	"""Face in 2D"""
	__slots__ = ('verts', 'edges', 'face', 'island', 'flipped', 'uvvertex_by_id')

	def __init__(self, face: Face, island: Island):
		"""Creace an UVFace from a Face and a fixed edge.
		face: Face to take coordinates from
		island: Island to register itself in
		fixed_edge: Edge to connect to (that already has UV coordinates)"""
		self.verts = list()
		self.face = face
		face.uvface = self
		self.island = island
		self.flipped = False  # a flipped UVFace has edges clockwise
		
		rot = z_up_matrix(face.normal)
		self.uvvertex_by_id = dict()  # link vertex id -> UVVertex
		for vertex in face.verts:
			uvvertex = UVVertex(rot * vertex.co, vertex)
			self.verts.append(uvvertex)
			self.uvvertex_by_id[vertex.index] = uvvertex
		self.edges = list()
		edge_by_verts = dict()
		for edge in face.edges:
			edge_by_verts[(edge.va.index, edge.vb.index)] = edge
			edge_by_verts[(edge.vb.index, edge.va.index)] = edge
		for va, vb in pairs(self.verts):
			uvedge = UVEdge(va, vb, island, self, edge_by_verts[(va.vertex.index, vb.vertex.index)])
			self.edges.append(uvedge)
			island.edges.add(uvedge)


class Marker:
	"""Various graphical elements linked to the net, but not being parts of the mesh"""
	pass


class Arrow(Marker):
	"""Arrow denoting the number of the edge it points to"""
	__slots__ = ('bounds', 'center', 'rot', 'text', 'size')

	def __init__(self, uvedge, size, index):
		self.text = str(index)
		edge = (uvedge.vb.co - uvedge.va.co) if not uvedge.uvface.flipped else (uvedge.va.co - uvedge.vb.co)
		self.center = (uvedge.va.co + uvedge.vb.co) / 2
		self.size = size
		sin, cos = edge.y / edge.length, edge.x / edge.length
		self.rot = M.Matrix(((cos, -sin), (sin, cos)))
		tangent = edge.normalized()
		normal = M.Vector((tangent.y, -tangent.x))
		self.bounds = [self.center, self.center + (1.2*normal + tangent)*size, self.center + (1.2*normal - tangent)*size]


class Sticker(Marker):
	"""Sticker face"""
	__slots__ = ('bounds', 'center', 'rot', 'text', 'width', 'vertices')

	def __init__(self, uvedge, default_width=0.005, index=None, target_island=None):
		"""Sticker is directly attached to the given UVEdge"""
		first_vertex, second_vertex = (uvedge.va, uvedge.vb) if not uvedge.uvface.flipped else (uvedge.vb, uvedge.va)
		edge = first_vertex.co - second_vertex.co
		sticker_width = min(default_width, edge.length / 2)
		other = uvedge.edge.other_uvedge(uvedge)  # This is the other uvedge - the sticking target
		
		other_first, other_second = (other.va, other.vb) if not other.uvface.flipped else (other.vb, other.va)
		other_edge = other_second.co - other_first.co
		# angle a is at vertex uvedge.va, b is at uvedge.vb
		cos_a = cos_b = 0.5
		sin_a = sin_b = 0.75**0.5
		# len_a is length of the side adjacent to vertex a, len_b likewise
		len_a = len_b = sticker_width / sin_a
		# fix overlaps with the most often neighbour - its sticking target
		if first_vertex == other_second:
			cos_a = max(cos_a, (edge*other_edge) / (edge.length**2))  # angles between pi/3 and 0
			sin_a = abs(1 - cos_a**2)**0.5
			len_b = min(len_a, (edge.length*sin_a) / (sin_a*cos_b + sin_b*cos_a))
			len_a = 0 if sin_a == 0 else min(sticker_width / sin_a, (edge.length - len_b*cos_b) / cos_a)
		elif second_vertex == other_first:
			cos_b = max(cos_b, (edge*other_edge) / (edge.length**2))  # angles between pi/3 and 0
			sin_b = abs(1 - cos_b**2)**0.5
			len_a = min(len_a, (edge.length*sin_b) / (sin_a*cos_b + sin_b*cos_a))
			len_b = 0 if sin_b == 0 else min(sticker_width / sin_b, (edge.length - len_a*cos_a) / cos_b)
		v3 = UVVertex(second_vertex.co + M.Matrix(((cos_b, -sin_b), (sin_b, cos_b))) * edge * len_b / edge.length)
		v4 = UVVertex(first_vertex.co + M.Matrix(((-cos_a, -sin_a), (sin_a, -cos_a))) * edge * len_a / edge.length)
		if v3.co != v4.co:
			self.vertices = [second_vertex, v3, v4, first_vertex]
		else:
			self.vertices = [second_vertex, v3, first_vertex]
		
		sin, cos = edge.y / edge.length, edge.x / edge.length
		self.rot = M.Matrix(((cos, -sin), (sin, cos)))
		self.width = sticker_width * 0.9
		if index and target_island is not uvedge.island:
			self.text = "{}:{}".format(target_island.abbreviation, index)
		else:
			self.text = index
		self.center = (uvedge.va.co + uvedge.vb.co) / 2 + self.rot*M.Vector((0, self.width*0.2))
		self.bounds = [v3.co, v4.co, self.center] if v3.co != v4.co else [v3.co, self.center]


class NumberAlone(Marker):
	"""Numbering inside the island describing edges to be sticked"""
	__slots__ = ('bounds', 'center', 'rot', 'text', 'size')

	def __init__(self, uvedge, index, default_size=0.005):
		"""Sticker is directly attached to the given UVEdge"""
		edge = (uvedge.va - uvedge.vb) if not uvedge.uvface.flipped else (uvedge.vb - uvedge.va)

		self.size = default_size
		sin, cos = edge.y / edge.length, edge.x / edge.length
		self.rot = M.Matrix(((cos, -sin), (sin, cos)))
		self.text = index
		self.center = (uvedge.va.co + uvedge.vb.co) / 2 - self.rot*M.Vector((0, self.size*1.2))
		self.bounds = [self.center]


class SVG:
	"""Simple SVG exporter"""

	def __init__(self, page_size_pixels: M.Vector, scale, style, pure_net=True):
		"""Initialize document settings.
		page_size_pixels: document dimensions in pixels
		pure_net: if True, do not use image"""
		self.page_size = page_size_pixels
		self.scale = scale
		self.pure_net = pure_net
		self.style = style
		self.margin = 0
	
	def format_vertex(self, vector, rot=1, pos=M.Vector((0, 0))):
		"""Return a string with both coordinates of the given vertex."""
		vector = rot*vector + pos
		return "{:.6f} {:.6f}".format(vector.x*self.scale + self.margin, (1-vector.y)*self.scale + self.margin)
	
	def write(self, mesh, filename):
		"""Write data to a file given by its name."""
		line_through = " L ".join  # used for formatting of SVG path data
		format_style = {'SOLID': "none", 'DOT': "0.2,4", 'DASH': "4,8", 'LONGDASH': "6,3", 'DASHDOT': "8,4,2,4"}
		rows = "\n".join

		def format_color(vec):
			return "#{:02x}{:02x}{:02x}".format(round(vec[0] * 255), round(vec[1] * 255), round(vec[2] * 255))

		def format_matrix(matrix):
			return " ".join(" ".join(map(str, column)) for column in matrix)
		
		def path_convert(string, relto=os_path.dirname(filename)):
			assert(os_path)  # check the module was imported
			string = os_path.relpath(string, relto)
			if os_path.sep != '/':
				string = string.replace(os_path.sep, '/')
			return string
		
		styleargs = {name: format_color(getattr(self.style, name)) for name in
			("outer_color", "outbg_color", "convex_color", "concave_color",
			"inbg_color", "sticker_fill", "sticker_color", "text_color")}
		styleargs.update({name: format_style[getattr(self.style, name)] for name in 
			("outer_style", "convex_style", "concave_style")})
		styleargs.update({name: getattr(self.style, attr)[3] for name, attr in
			(("outer_alpha", "outer_color"), ("outbg_alpha", "outbg_color"),
			("convex_alpha", "convex_color"), ("concave_alpha", "concave_color"),
			("inbg_alpha", "inbg_color"), ("sticker_alpha", "sticker_fill"),
			("text_alpha", "text_color"))})
		styleargs.update({name: getattr(self.style, name) for name in
			("outer_width", "convex_width", "concave_width", "sticker_width")})
		styleargs.update({"outbg_width": self.style.outer_width * self.style.outbg_width,
			"convexbg_width": self.style.convex_width * self.style.inbg_width,
			"concavebg_width": self.style.concave_width * self.style.inbg_width})
		for num, page in enumerate(mesh.pages):
			with open("{}_{}.svg".format(filename, page.name), 'w') as f:
				print(self.svg_base.format(width=self.page_size.x, height=self.page_size.y), file=f)
				print(self.css_base.format(**styleargs), file=f)
				if page.image_path:
					print(self.image_linked_tag.format(
						pos="{0} {0}".format(self.margin),
						width=self.page_size.x - 2*self.margin,
						height=self.page_size.y - 2*self.margin,
						path=path_convert(page.image_path)),
						file=f)
				if len(page.islands) > 1:
					print("<g>", file=f)
				for island in page.islands:
					print("<g>", file=f)
					if island.image_path:
						print(self.image_linked_tag.format(
							pos=self.format_vertex(island.pos + M.Vector((0, island.bounding_box.y))),
							width=island.bounding_box.x*self.scale,
							height=island.bounding_box.y*self.scale,
							path=path_convert(island.image_path)),
							file=f)
					elif island.embedded_image:
						print(self.image_embedded_tag.format(
								pos=self.format_vertex(island.pos + M.Vector((0, island.bounding_box.y))),
								width=island.bounding_box.x*self.scale,
								height=island.bounding_box.y*self.scale,
								path=island.image_path),
							island.embedded_image, "'/>",
							file=f, sep="")

					rot = M.Matrix.Rotation(island.angle, 2)
					pos = island.pos + island.offset
					
					data_outer, data_convex, data_concave = list(), list(), list()
					for uvedge in island.edges:
						edge = uvedge.edge
						data_uvedge = "M " + line_through((self.format_vertex(vertex.co, rot, pos) for vertex in (uvedge.va, uvedge.vb)))
						if not edge.is_cut(uvedge.uvface.face):
							# each uvedge is in two opposite-oriented variants; we want to add each only once
							if uvedge.uvface.flipped != (uvedge.va.vertex.index > uvedge.vb.vertex.index):
								if edge.angle > 0.01:
									data_convex.append(data_uvedge)
								elif edge.angle < -0.01:
									data_concave.append(data_uvedge)
						else:
							data_outer.append(data_uvedge)
					if island.is_inside_out:
						data_convex, data_concave = data_concave, data_convex
					
					if data_convex:
						if not self.pure_net and self.style.use_inbg:
							print("<path class='convex_background' d='", rows(data_convex), "'/>", file=f)
						print("<path class='convex' d='", rows(data_convex), "'/>", file=f)
					if data_concave:
						if not self.pure_net and self.style.use_inbg:
							print("<path class='concave_background' d='", rows(data_concave), "'/>", file=f)
						print("<path class='concave' d='", rows(data_concave), "'/>", file=f)
					if data_outer:
						if not self.pure_net and self.style.use_outbg:
							print("<path class='outer_background' d='", rows(data_outer), "'/>", file=f)
						print("<path class='outer' d='", rows(data_outer), "'/>", file=f)
					
					if island.title:
						print(self.text_tag.format(
							x=self.scale * (island.bounding_box.x*0.5 + island.pos.x) + self.margin,
							y=self.scale * (1 - island.pos.y) + self.margin,
							label=island.title), file=f)
					data_markers = list()
					
					for marker in island.markers:
						if isinstance(marker, Sticker):
							if self.do_create_stickers:
								text = self.text_scaled_tag.format(
									label=marker.text,
									pos=self.format_vertex(marker.center, rot, pos),
									mat=format_matrix(marker.width * island.scale * self.scale * rot * marker.rot)) if marker.text else ""
								data_markers.append("<g><path class='sticker' d='M {data} Z'/>{text}</g>".format(
									data=line_through((self.format_vertex(vertex.co, rot, pos) for vertex in marker.vertices)),
									text=text))
							elif marker.text:
								data_markers.append(self.text_scaled_tag.format(
									label=marker.text,
									pos=self.format_vertex(marker.center, rot, pos),
									mat=format_matrix(marker.width * island.scale * self.scale * rot * marker.rot)))
						elif isinstance(marker, Arrow):
							size = marker.size * island.scale * self.scale
							position = marker.center + marker.rot*marker.size*island.scale*M.Vector((0, -0.9))
							data_markers.append(self.arrow_marker_tag.format(
								index=marker.text,
								arrow_pos=self.format_vertex(marker.center, rot, pos),
								scale=size,
								pos=self.format_vertex(position, rot, pos - marker.size*island.scale*M.Vector((0, 0.4))),
								mat=format_matrix(size * rot * marker.rot)))
						elif isinstance(marker, NumberAlone):
							size = marker.size * island.scale * self.scale
							data_markers.append(self.text_scaled_tag.format(
								label=marker.text,
								pos=self.format_vertex(marker.center, rot, pos),
								mat=format_matrix(size * rot * marker.rot)))
					if data_markers:
						# Stickers are separate paths in one group
						print("<g>", rows(data_markers), "</g>", file=f)
					print("</g>", file=f)
				
				if len(page.islands) > 1:
					print("</g>", file=f)
				print("</svg>", file=f)
	
	image_linked_tag = "<image transform='translate({pos})' width='{width}' height='{height}' xlink:href='{path}'/>"
	image_embedded_tag = "<image transform='translate({pos})' width='{width}' height='{height}' xlink:href='data:image/png;base64,"
	text_tag = "<text transform='translate({x} {y})'><tspan>{label}</tspan></text>"
	text_scaled_tag = "<text class='scaled' transform='matrix({mat} {pos})'><tspan>{label}</tspan></text>"
	arrow_marker_tag = "<g><path transform='matrix({mat} {arrow_pos})' class='arrow' d='M 0 0 L 1 1 L 0 0.25 L -1 1 Z'/>" \
		"<text class='scaled' transform='matrix({scale} 0 0 {scale} {pos})'><tspan>{index}</tspan></text></g>"
	
	svg_base = """<?xml version='1.0' encoding='UTF-8' standalone='no'?>
	<svg xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink' version='1.1'
	x='0px' y='0px' width='{width}px' height='{height}px'>"""
	
	css_base = """<style type="text/css">
	path {{
		fill: none;
		stroke-width: {outer_width:.2}px;
		stroke-linecap: square;
		stroke-linejoin: bevel;
		stroke-dasharray: none;
	}}
	path.outer {{
		stroke: {outer_color};
		stroke-dasharray: {outer_style};
		stroke-dashoffset: 0;
		stroke-width: {outer_width:.2}px;
		stroke-opacity: {outer_alpha:.2};
	}}
	path.convex {{
		stroke: {convex_color};
		stroke-dasharray: {convex_style};
		stroke-dashoffset:0;
		stroke-width:{convex_width:.2}px;
		stroke-opacity: {convex_alpha:.2}
	}}
	path.concave {{
		stroke: {concave_color};
		stroke-dasharray: {concave_style};
		stroke-dashoffset: 0;
		stroke-width: {concave_width:.2}px;
		stroke-opacity: {concave_alpha:.2}
	}}
	path.outer_background {{
		stroke: {outbg_color};
		stroke-opacity: {outbg_alpha};
		stroke-width: {outbg_width:.2}px
	}}
	path.convex_background {{
		stroke: {inbg_color};
		stroke-opacity: {inbg_alpha};
		stroke-width: {convexbg_width:.2}px
	}}
	path.concave_background {{
		stroke:{inbg_color};
		stroke-opacity:{inbg_alpha};
		stroke-width:{concavebg_width:.2}px
	}}
	path.sticker {{
		fill: {sticker_fill};
		stroke: {sticker_color};
		fill-opacity: {sticker_alpha:.2};
		stroke-width:{sticker_width:.2};
		stroke-opacity: 1
	}}
	path.arrow {{
		fill: #000;
	}}
	text {{
		font-size: 12px;
		font-style: normal;
		fill: {text_color};
		fill-opacity: {text_alpha:.2};
		stroke: none;
	}}
	text.scaled {{
		font-size: 1px;
	}}
	tspan {{
		text-anchor:middle;
	}}
	</style>"""
	


class Unfold(bpy.types.Operator):
	"""Blender Operator: unfold the selected object."""

	bl_idname = "mesh.unfold"
	bl_label = "Unfold"
	bl_description = "Mark seams so that the mesh can be exported as a paper model"
	bl_options = {'REGISTER', 'UNDO'}
	edit = bpy.props.BoolProperty(name="", description="", default=False, options={'HIDDEN'})
	priority_effect_convex = bpy.props.FloatProperty(name="Priority Convex",
		description="Priority effect for edges in convex angles",
		default=default_priority_effect['CONVEX'], soft_min=-1, soft_max=10, subtype='FACTOR')
	priority_effect_concave = bpy.props.FloatProperty(name="Priority Concave",
		description="Priority effect for edges in concave angles",
		default=default_priority_effect['CONCAVE'], soft_min=-1, soft_max=10, subtype='FACTOR')
	priority_effect_length = bpy.props.FloatProperty(name="Priority Length",
		description="Priority effect of edge length",
		default=default_priority_effect['LENGTH'], soft_min=-10, soft_max=1, subtype='FACTOR')
	do_create_uvmap = bpy.props.BoolProperty(name="Create UVMap",
		description="Create a new UV Map showing the islands and page layout", default=False)
	unfolder = None
	
	@classmethod
	def poll(cls, context):
		return context.active_object and context.active_object.type == "MESH"
		
	def draw(self, context):
		layout = self.layout
		col = layout.column()
		col.active = not self.unfolder or len(self.unfolder.mesh.data.uv_textures) < 8
		col.prop(self.properties, "do_create_uvmap")
		layout.label(text="Edge Cutting Factors:")
		col = layout.column(align=True)
		col.label(text="Face Angle:")
		col.prop(self.properties, "priority_effect_convex", text="Convex")
		col.prop(self.properties, "priority_effect_concave", text="Concave")
		layout.prop(self.properties, "priority_effect_length", text="Edge Length")
	
	def execute(self, context):
		sce = bpy.context.scene
		settings = sce.paper_model
		recall_mode = context.object.mode
		bpy.ops.object.mode_set(mode='OBJECT')
		recall_display_islands, sce.paper_model.display_islands = sce.paper_model.display_islands, False
		
		ob = context.active_object
		mesh = context.active_object.data
		
		page_size = M.Vector((settings.output_size_x, settings.output_size_y)) if settings.limit_by_page else None
		priority_effect = {'CONVEX': self.priority_effect_convex,'CONCAVE': self.priority_effect_concave, 'LENGTH': self.priority_effect_length}
		self.unfolder = unfolder = Unfolder(ob)
		unfolder.prepare(page_size=page_size, mark_seams=True, create_uvmap=self.do_create_uvmap, priority_effect=priority_effect, scale=sce.unit_settings.scale_length/settings.scale)
		if mesh.paper_island_list:
			self.unfolder.copy_island_names(mesh.paper_island_list)

		island_list = mesh.paper_island_list
		island_list.clear()  # remove previously defined islands
		for island in unfolder.mesh.islands:
			# add islands to UI list and set default descriptions
			list_item = island_list.add()
			
			# add faces' IDs to the island
			for uvface in island.faces:
				lface = list_item.faces.add()
				lface.id = uvface.face.index
			
			# name must be set afterwards because it invokes an update callback
			list_item["abbreviation"] = island.abbreviation or "?"
			list_item.label = island.label or "No Name"
		
		mesh.paper_island_index = -1
		
		unfolder.mesh.data.show_edge_seams = True
		bpy.ops.object.mode_set(mode=recall_mode)
		sce.paper_model.display_islands = recall_display_islands
		return {'FINISHED'}


class ClearAllSeams(bpy.types.Operator):
	"""Blender Operator: clear all seams of the active Mesh and all its unfold data"""

	bl_idname = "mesh.clear_all_seams"
	bl_label = "Clear All Seams"
	bl_description = "Clear all the seams and unfolded islands of the active object"

	@classmethod
	def poll(cls, context):
		return context.active_object and context.active_object.type == 'MESH'
	
	def execute(self, context):
		ob = context.active_object
		mesh = ob.data
		
		for edge in mesh.edges:
			edge.use_seam = False
		mesh.paper_island_list.clear()
		
		return {'FINISHED'}


def page_size_preset_changed(self, context):
	"""Update the actual document size to correct values"""
	if self.page_size_preset == 'A4':
		self.output_size_x = 0.210
		self.output_size_y = 0.297
	elif self.page_size_preset == 'A3':
		self.output_size_x = 0.297
		self.output_size_y = 0.420
	elif self.page_size_preset == 'US_LETTER':
		self.output_size_x = 0.216
		self.output_size_y = 0.279
	elif self.page_size_preset == 'US_LEGAL':
		self.output_size_x = 0.216
		self.output_size_y = 0.356


class PaperModelStyle(bpy.types.PropertyGroup):
	line_styles = [
		('SOLID', "Solid (----)", "Solid line"),
		('DOT', "Dots (. . .)", "Dotted line"),
		('DASH', "Short Dashes (- - -)", "Solid line"),
		('LONGDASH', "Long Dashes (-- --)", "Solid line"),
		('DASHDOT', "Dash-dotted (-- .)", "Solid line")
	]
	outer_color = bpy.props.FloatVectorProperty(name="Outer Lines",
		description="Color of net outline",
		default=(0.0, 0.0, 0.0, 1.0), min=0, max=1, subtype='COLOR', size=4)
	outer_style = bpy.props.EnumProperty(name="Outer Lines Drawing Style",
		description="Drawing style of net outline",
		default='SOLID', items=line_styles)
	outer_width = bpy.props.FloatProperty(name="Outer Lines Thickness",
		description="Thickness of net outline, in pixels",
		default=1.5, min=0, soft_max=10, precision=1)
	use_outbg = bpy.props.BoolProperty(name="Highlight Outer Lines",
		description="Add another line below every line to improve contrast",
		default=True)
	outbg_color = bpy.props.FloatVectorProperty(name="Outer Highlight",
		description="Color of the highlight for outer lines",
		default=(1.0, 1.0, 1.0, 1.0), min=0, max=1, subtype='COLOR', size=4)
	outbg_width = bpy.props.FloatProperty(name="Outer Highlight Scale",
		description="Thickness of the highlighting lines as a multiple of the outer line",
		default=1.5, min=1, soft_max=3, subtype='FACTOR')
	
	convex_color = bpy.props.FloatVectorProperty(name="Inner Convex Lines",
		description="Color of lines to be folded to a convex angle",
		default=(0.0, 0.0, 0.0, 1.0), min=0, max=1, subtype='COLOR', size=4)
	convex_style = bpy.props.EnumProperty(name="Convex Lines Drawing Style",
		description="Drawing style of lines to be folded to a convex angle",
		default='DASH', items=line_styles)
	convex_width = bpy.props.FloatProperty(name="Convex Lines Thickness",
		description="Thickness of concave lines, in pixels",
		default=1, min=0, soft_max=10, precision=1)
	concave_color = bpy.props.FloatVectorProperty(name="Inner Concave Lines",
		description="Color of lines to be folded to a concave angle",
		default=(0.0, 0.0, 0.0, 1.0), min=0, max=1, subtype='COLOR', size=4)
	concave_style = bpy.props.EnumProperty(name="Concave Lines Drawing Style",
		description="Drawing style of lines to be folded to a concave angle",
		default='DASHDOT', items=line_styles)
	concave_width = bpy.props.FloatProperty(name="Concave Lines Thickness",
		description="Thickness of concave lines, in pixels",
		default=1, min=0, soft_max=10, precision=1)
	use_inbg = bpy.props.BoolProperty(name="Highlight Inner Lines",
		description="Add another line below every line to improve contrast",
		default=True)
	inbg_color = bpy.props.FloatVectorProperty(name="Inner Highlight",
		description="Color of the highlight for inner lines",
		default=(1.0, 1.0, 1.0, 1.0), min=0, max=1, subtype='COLOR', size=4)
	inbg_width = bpy.props.FloatProperty(name="Inner Highlight Scale",
		description="Thickness of the highlighting lines as a multiple of the inner line",
		default=1, min=1, soft_max=3, subtype='FACTOR')
	
	sticker_fill = bpy.props.FloatVectorProperty(name="Tabs Fill",
		description="Fill color of sticking tabs",
		default=(1.0, 1.0, 1.0, 0.4), min=0, max=1, subtype='COLOR', size=4)
	sticker_color = bpy.props.FloatVectorProperty(name="Tabs Outline",
		description="Outline color of sticking tabs",
		default=(0.0, 0.0, 0.0), min=0, max=1, subtype='COLOR', size=3)
	sticker_width = bpy.props.FloatProperty(name="Tabs Outline Thickness",
		description="Thickness of tabs outer line, in pixels",
		default=1, min=0, soft_max=10, precision=1)
	text_color = bpy.props.FloatVectorProperty(name="Text Color",
		description="Color of all text used in the document",
		default=(0.0, 0.0, 0.0, 1.0), min=0, max=1, subtype='COLOR', size=4)
bpy.utils.register_class(PaperModelStyle)


class ExportPaperModel(bpy.types.Operator):
	"""Blender Operator: save the selected object's net and optionally bake its texture"""

	bl_idname = "export_mesh.paper_model"
	bl_label = "Export Paper Model"
	bl_description = "Export the selected object's net and optionally bake its texture"
	filepath = bpy.props.StringProperty(name="File Path",
		description="Target file to save the SVG")
	filename = bpy.props.StringProperty(name="File Name",
		description="Name of the file")
	directory = bpy.props.StringProperty(name="Directory",
		description="Directory of the file")
	page_size_preset = bpy.props.EnumProperty(name="Page Size",
		description="Size of the exported document",
		default='A4', update=page_size_preset_changed, items=[
			('USER', "User defined", "User defined paper size"),
			('A4', "A4", "International standard paper size"),
			('A3', "A3", "International standard paper size"),
			('US_LETTER', "Letter", "North American paper size"),
			('US_LEGAL', "Legal", "North American paper size")
		])
	output_size_x = bpy.props.FloatProperty(name="Page Width",
		description="Width of the exported document",
		default=0.210, soft_min=0.105, soft_max=0.841, subtype="UNSIGNED", unit="LENGTH")
	output_size_y = bpy.props.FloatProperty(name="Page Height",
		description="Height of the exported document",
		default=0.297, soft_min=0.148, soft_max=1.189, subtype="UNSIGNED", unit="LENGTH")
	output_margin = bpy.props.FloatProperty(name="Page Margin",
		description="Distance from page borders to the printable area",
		default=0.005, min=0, soft_max=0.1, subtype="UNSIGNED", unit="LENGTH")
	output_dpi = bpy.props.FloatProperty(name="Unfolder DPI",
		description="Resolution of images and lines in pixels per inch",
		default=90, min=1, soft_min=30, soft_max=600, subtype="UNSIGNED")
	output_type = bpy.props.EnumProperty(name="Textures",
		description="Source of a texture for the model",
		default='NONE', items=[
			('NONE', "No Texture", "Export the net only"),
			('TEXTURE', "Face Texture", "Export the active texture as it is in the 3D View"),
			('RENDER', "Full Render", "Render the material of the model, including all illumination"),
			('SELECTED_TO_ACTIVE', "Selected to Active", "Use the selected surrounding objects as a texture")
		])
	do_create_stickers = bpy.props.BoolProperty(name="Create Tabs",
		description="Create gluing tabs around the net (useful for paper)",
		default=True)
	do_create_numbers = bpy.props.BoolProperty(name="Create Numbers",
		description="Enumerate edges to make it clear which edges should be sticked together",
		default=True)
	sticker_width = bpy.props.FloatProperty(name="Tabs and Text Size",
		description="Width of gluing tabs and their numbers",
		default=0.005, soft_min=0, soft_max=0.05, subtype="UNSIGNED", unit="LENGTH")
	image_packing = bpy.props.EnumProperty(name="Image Packing Method",
		description="Method of attaching baked image(s) to the SVG",
		default='PAGE_LINK', items=[
			('PAGE_LINK', "Single Linked", "Bake one image per page of output"),
			('ISLAND_LINK', "Linked", "Bake images separately for each island and save them in a directory"),
			('ISLAND_EMBED', "Embedded", "Bake images separately for each island and embed them into the SVG")
		])
	scale = bpy.props.FloatProperty(name="Scale",
		description="Divisor of all dimensions when exporting",
		default=1, soft_min=1.0, soft_max=10000.0, subtype='UNSIGNED', precision=0)
	do_create_uvmap = bpy.props.BoolProperty(name="Create UVMap",
		description="Create a new UV Map showing the islands and page layout",
		default=False)
	ui_expanded_document = bpy.props.BoolProperty(name="Show Document Settings Expanded",
		description="Shows the box 'Document Settings' expanded in user interface",
		default=True)
	ui_expanded_style = bpy.props.BoolProperty(name="Show Style Settings Expanded",
		description="Shows the box 'Colors and Style' expanded in user interface",
		default=False)
	style = bpy.props.PointerProperty(type=PaperModelStyle)
	
	unfolder = None
	largest_island_ratio = 0
	
	@classmethod
	def poll(cls, context):
		return context.active_object and context.active_object.type == 'MESH'
	
	def execute(self, context):
		try:
			if self.object.data.paper_island_list:
				self.unfolder.copy_island_names(self.object.data.paper_island_list)
			self.unfolder.save(self.properties)
			self.report({'INFO'}, "Saved a {}-page document".format(len(self.unfolder.mesh.pages)))
			return {'FINISHED'}
		except UnfoldError as error:
			self.report(type={'ERROR_INVALID_INPUT'}, message=error.args[0])
			return {'CANCELLED'}
		except:
			raise
	
	def get_scale_ratio(self, sce):
		margin = self.output_margin + self.sticker_width + 1e-5
		if min(self.output_size_x, self.output_size_y) <= 2 * margin:
			return False
		output_inner_size = M.Vector((self.output_size_x - 2*margin, self.output_size_y - 2*margin))
		ratio = self.unfolder.mesh.largest_island_ratio(output_inner_size)
		return ratio * sce.unit_settings.scale_length / self.scale
	
	def invoke(self, context, event):
		sce = context.scene
		
		self.scale = sce.paper_model.scale
		self.object = context.active_object
		self.unfolder = Unfolder(self.object)
		self.unfolder.prepare(create_uvmap=self.do_create_uvmap, scale=sce.unit_settings.scale_length/self.scale)
		scale_ratio = self.get_scale_ratio(sce)
		if scale_ratio > 1:
			self.scale *= scale_ratio
		wm = context.window_manager
		wm.fileselect_add(self)
		return {'RUNNING_MODAL'}
	
	def draw(self, context):
		layout = self.layout
		# a little hack: this prints out something like "Scale: 1: 72"
		layout.prop(self.properties, "scale", text="Scale: 1")
		scale_ratio = self.get_scale_ratio(context.scene)
		if scale_ratio > 1:
			layout.label(text="An island is roughly {:.1f}x bigger than page".format(scale_ratio), icon="ERROR")
		elif scale_ratio > 0:
			layout.label(text="Largest island is roughly 1/{:.1f} of page".format(1 / scale_ratio))
		layout.prop(self.properties, "do_create_uvmap")

		box = layout.box()
		row = box.row(align=True)
		row.prop(self.properties, "ui_expanded_document", text="",
			icon=('TRIA_DOWN' if self.ui_expanded_document else 'TRIA_RIGHT'), emboss=False)
		row.label(text="Document Settings")
		
		if self.ui_expanded_document:
			box.prop(self.properties, "page_size_preset")
			col = box.column(align=True)
			col.active = self.page_size_preset == 'USER'
			col.prop(self.properties, "output_size_x")
			col.prop(self.properties, "output_size_y")
			box.prop(self.properties, "output_margin")
			box.prop(self.properties, "output_dpi")
			col = box.column()
			col.prop(self.properties, "do_create_stickers")
			col.prop(self.properties, "do_create_numbers")
			col = box.column()
			col.active = self.do_create_stickers or self.do_create_numbers
			col.prop(self.properties, "sticker_width")
			
			box.prop(self.properties, "output_type")
			col = box.column()
			col.active = self.output_type != 'NONE'
			if len(self.object.data.uv_textures) == 8:
				col.label(text="No UV slots left, No Texture is the only option.", icon='ERROR')
			elif context.scene.render.engine != 'BLENDER_RENDER' and self.output_type != 'NONE':
				col.label(text="Blender Internal engine will be used for texture baking.", icon='ERROR')
			col.prop(self.properties, "image_packing", text="Images")
		
		box = layout.box()
		row = box.row(align=True)
		row.prop(self.properties, "ui_expanded_style", text="",
			icon=('TRIA_DOWN' if self.ui_expanded_style else 'TRIA_RIGHT'), emboss=False)
		row.label(text="Colors and Style")
		
		if self.ui_expanded_style:
			col = box.column()
			col.prop(self.style, "outer_color")
			col.prop(self.style, "outer_width", text="Width (pixels)")
			col.prop(self.style, "outer_style", text="Style")
			col = box.column()
			col.active = self.output_type != 'NONE'
			col.prop(self.style, "use_outbg", text="Outer Lines Highlight:")
			sub = col.column()
			sub.active = self.output_type != 'NONE' and self.style.use_outbg
			sub.prop(self.style, "outbg_color", text="")
			sub.prop(self.style, "outbg_width", text="Relative width")
			col = box.column()
			col.prop(self.style, "convex_color")
			col.prop(self.style, "convex_width", text="Width (pixels)")
			col.prop(self.style, "convex_style", text="Style")
			col = box.column()
			col.prop(self.style, "concave_color")
			col.prop(self.style, "concave_width", text="Width (pixels)")
			col.prop(self.style, "concave_style", text="Style")
			col = box.column()
			col.active = self.output_type != 'NONE'
			col.prop(self.style, "use_inbg", text="Inner Lines Highlight:")
			sub = col.column()
			sub.active = self.output_type != 'NONE' and self.style.use_inbg
			sub.prop(self.style, "inbg_color", text="")
			sub.prop(self.style, "inbg_width", text="Relative width")
			col = box.column()
			col.active = self.do_create_stickers
			col.prop(self.style, "sticker_fill")
			col.prop(self.style, "sticker_color")
			col.prop(self.style, "sticker_width", text="Outline width (pixels)")
			box.prop(self.style, "text_color")


def menu_func(self, context):
	self.layout.operator("export_mesh.paper_model", text="Paper Model (.svg)")


class VIEW3D_PT_paper_model_tools(bpy.types.Panel):
	bl_label = "Tools"
	bl_space_type = "VIEW_3D"
	bl_region_type = "TOOLS"
	bl_category = "Paper Model"

	def draw(self, context):
		layout = self.layout
		sce = context.scene
		obj = context.active_object
		mesh = obj.data if obj and obj.type == 'MESH' else None
		
		layout.operator("export_mesh.paper_model")
		
		col = layout.column(align=True)
		col.label("Customization:")
		col.operator("mesh.unfold")
		
		if context.mode == 'EDIT_MESH':
			row = layout.row(align=True)
			row.operator("mesh.mark_seam", text="Mark Seam").clear = False
			row.operator("mesh.mark_seam", text="Clear Seam").clear = True
		else:
			layout.operator("mesh.clear_all_seams")
		
		layout.prop(sce.paper_model, "scale", text="Model Scale: 1")
		
		col = layout.column(align=True)
		col.prop(sce.paper_model, "limit_by_page")
		sub = col.column(align=True)
		sub.active = sce.paper_model.limit_by_page
		sub.prop(sce.paper_model, "output_size_x")
		sub.prop(sce.paper_model, "output_size_y")


class VIEW3D_PT_paper_model_islands(bpy.types.Panel):
	bl_label = "Islands"
	bl_space_type = "VIEW_3D"
	bl_region_type = "TOOLS"
	bl_category = "Paper Model"
	
	def draw(self, context):
		layout = self.layout
		sce = context.scene
		obj = context.active_object
		mesh = obj.data if obj and obj.type == 'MESH' else None
		
		if mesh and mesh.paper_island_list:
			layout.label(text="1 island:" if len(mesh.paper_island_list) == 1 else
				"{} islands:".format(len(mesh.paper_island_list)))
			layout.template_list('UI_UL_list', 'paper_model_island_list', mesh,
				'paper_island_list', mesh, 'paper_island_index', rows=1, maxrows=5)
			if mesh.paper_island_index >= 0:
				list_item = mesh.paper_island_list[mesh.paper_island_index]
				sub = layout.column(align=True)
				sub.prop(list_item, "label")
				sub.prop(list_item, "auto_abbrev")
				row = sub.row()
				row.active = not list_item.auto_abbrev
				row.prop(list_item, "abbreviation")
		else:
			layout.label(text="Not unfolded")
			layout.box().label("Use the 'Unfold' tool")
		sub = layout.column(align=True)
		sub.active = bool(mesh and mesh.paper_island_list)
		sub.prop(sce.paper_model, "display_islands", icon='RESTRICT_VIEW_OFF')
		row = sub.row(align=True)
		row.active = bool(sce.paper_model.display_islands and mesh and mesh.paper_island_list)
		row.prop(sce.paper_model, "islands_alpha", slider=True)


def display_islands(self, context):
	# TODO: save the vertex positions and don't recalculate them always?
	ob = context.active_object
	if not ob or ob.type != 'MESH':
		return
	mesh = ob.data
	if not mesh.paper_island_list or mesh.paper_island_index == -1:
		return
	
	bgl.glMatrixMode(bgl.GL_PROJECTION)
	perspMatrix = context.space_data.region_3d.perspective_matrix
	perspBuff = bgl.Buffer(bgl.GL_FLOAT, (4, 4), perspMatrix.transposed())
	bgl.glLoadMatrixf(perspBuff)
	bgl.glMatrixMode(bgl.GL_MODELVIEW)
	objectBuff = bgl.Buffer(bgl.GL_FLOAT, (4, 4), ob.matrix_world.transposed())
	bgl.glLoadMatrixf(objectBuff)
	bgl.glEnable(bgl.GL_BLEND)
	bgl.glBlendFunc(bgl.GL_SRC_ALPHA, bgl.GL_ONE_MINUS_SRC_ALPHA)
	bgl.glEnable(bgl.GL_POLYGON_OFFSET_FILL)
	bgl.glPolygonOffset(0, -10)  # offset in Zbuffer to remove flicker
	bgl.glPolygonMode(bgl.GL_FRONT_AND_BACK, bgl.GL_FILL)
	bgl.glColor4f(1.0, 0.4, 0.0, self.islands_alpha)
	island = mesh.paper_island_list[mesh.paper_island_index]
	for lface in island.faces:
		face = mesh.polygons[lface.id]
		bgl.glBegin(bgl.GL_POLYGON)
		for vertex_id in face.vertices:
			vertex = mesh.vertices[vertex_id]
			bgl.glVertex4f(*vertex.co.to_4d())
		bgl.glEnd()
	bgl.glPolygonOffset(0.0, 0.0)
	bgl.glDisable(bgl.GL_POLYGON_OFFSET_FILL)
	bgl.glLoadIdentity()
display_islands.handle = None


def display_islands_changed(self, context):
	"""Switch highlighting islands on/off"""
	if self.display_islands:
		if not display_islands.handle:
			display_islands.handle = bpy.types.SpaceView3D.draw_handler_add(display_islands, (self, context), 'WINDOW', 'POST_VIEW')
	else:
		if display_islands.handle:
			bpy.types.SpaceView3D.draw_handler_remove(display_islands.handle, 'WINDOW')
			display_islands.handle = None


def label_changed(self, context):
	"""The labelling of an island was changed"""
	# accessing properties via [..] to avoid a recursive call after the update
	if self.auto_abbrev:
		self["abbreviation"] = "".join(first_letters(self.label)).upper()
	elif len(self.abbreviation) > 3:
		self["abbreviation"] = self.abbreviation[:3]
	self.name = "[{}] {} ({} {})".format(self.abbreviation, self.label, len(self.faces), "faces" if len(self.faces) > 1 else "face")


class FaceList(bpy.types.PropertyGroup):
	id = bpy.props.IntProperty(name="Face ID")


class IslandList(bpy.types.PropertyGroup):
	faces = bpy.props.CollectionProperty(type=FaceList, name="Faces",
		description="Faces belonging to this island")
	label = bpy.props.StringProperty(name="Label",
		description="Label on this island",
		default="", update=label_changed)
	abbreviation = bpy.props.StringProperty(name="Abbreviation",
		description="Three-letter label to use when there is not enough space",
		default="", update=label_changed)
	auto_abbrev = bpy.props.BoolProperty(name="Auto Abbreviation",
		description="Generate the abbreviation automatically",
		default=True, update=label_changed)
bpy.utils.register_class(FaceList)
bpy.utils.register_class(IslandList)


class PaperModelSettings(bpy.types.PropertyGroup):
	display_islands = bpy.props.BoolProperty(name="Highlight selected island",
		description="Highlight faces corresponding to the selected island in the 3D View",
		options={'SKIP_SAVE'}, update=display_islands_changed)
	islands_alpha = bpy.props.FloatProperty(name="Opacity",
		description="Opacity of island highlighting", min=0.0, max=1.0, default=0.3)
	limit_by_page = bpy.props.BoolProperty(name="Limit Island Size",
		description="Do not create islands larger than given dimensions")
	output_size_x = bpy.props.FloatProperty(name="Width",
		description="Maximal width of an island",
		default=0.2, soft_min=0.105, soft_max=0.841, subtype="UNSIGNED", unit="LENGTH")
	output_size_y = bpy.props.FloatProperty(name="Height",
		description="Maximal height of an island",
		default=0.29, soft_min=0.148, soft_max=1.189, subtype="UNSIGNED", unit="LENGTH")
	scale = bpy.props.FloatProperty(name="Scale",
		description="Divisor of all dimensions when exporting",
		default=1, soft_min=1.0, soft_max=10000.0, subtype='UNSIGNED', precision=0)
bpy.utils.register_class(PaperModelSettings)


def register():
	bpy.utils.register_module(__name__)

	bpy.types.Scene.paper_model = bpy.props.PointerProperty(type=PaperModelSettings,
		name="Paper Model",
		description="Settings of the Export Paper Model script",
		options={'SKIP_SAVE'})
	bpy.types.Mesh.paper_island_list = bpy.props.CollectionProperty(type=IslandList,
		name="Island List", description="")
	bpy.types.Mesh.paper_island_index = bpy.props.IntProperty(name="Island List Index",
		default=-1, min=-1, max=100, options={'SKIP_SAVE'})
	bpy.types.INFO_MT_file_export.append(menu_func)


def unregister():
	bpy.utils.unregister_module(__name__)
	bpy.types.INFO_MT_file_export.remove(menu_func)
	if display_islands.handle:
		bpy.types.SpaceView3D.draw_handler_remove(display_islands.handle, 'WINDOW')
		display_islands.handle = None

if __name__ == "__main__":
	register()
