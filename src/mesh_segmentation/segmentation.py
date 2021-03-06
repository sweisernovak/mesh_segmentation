import bpy
import mathutils
import math
import numpy
import scipy.linalg
import scipy.cluster
import scipy.sparse
import scipy.sparse.csgraph
import scipy.sparse.linalg

# Controls weight of geodesic to angular distance. Values closer to 0 give
# the angular distance more importance, values closer to 1 give the geodesic
# distance more importance
delta = None

# Weight of convexity. Values close to zero give more importance to concave
# angles, values close to 1 treat convex and concave angles more equally
eta = None


def _face_center(mesh, face):
    """Computes the coordinates of the center of the given face"""
    center = mathutils.Vector()
    for vert in face.vertices:
        center += mesh.vertices[vert].co
    return center/len(face.vertices)


def _geodesic_distance(mesh, face1, face2, edge):
    """Computes the geodesic distance over the given edge between
    the two adjacent faces face1 and face2"""
    edge_center = (mesh.vertices[edge[0]].co + mesh.vertices[edge[1]].co)/2
    return (edge_center - _face_center(mesh, face1)).length + \
           (edge_center - _face_center(mesh, face2)).length


def _angular_distance(mesh, face1, face2):
    """Computes the angular distance of the given adjacent faces"""
    angular_distance = (1 - math.cos(mathutils.Vector.angle(face1.normal,
                                                            face2.normal)))
    if (face1.normal.dot(_face_center(mesh, face2) -
                         _face_center(mesh, face1))) < 0:
        # convex angles are not that bad so scale down distance a bit
        angular_distance *= eta
    return angular_distance


def _create_distance_matrix(mesh):
    """Creates the matrix of the angular and geodesic distances
    between all adjacent faces. The i,j-th entry of the returned
    matrices contains the distance between the i-th and j-th face.
    """

    faces = mesh.polygons
    l = len(faces)

    # map from edge-key to adjacent faces
    adj_faces_map = {}
    # find adjacent faces by iterating edges
    for index, face in enumerate(faces):
        for edge in face.edge_keys:
            if edge in adj_faces_map:
                adj_faces_map[edge].append(index)
            else:
                adj_faces_map[edge] = [index]

    # helping vectors to create sparse matrix later on
    row_indices = []
    col_indices = []
    Gval = []  # values for matrix of angular distances
    Aval = []  # values for matrix of geodesic distances
    # iterate adjacent faces and calculate distances
    for edge, adj_faces in adj_faces_map.items():
        if len(adj_faces) == 2:
            i = adj_faces[0]
            j = adj_faces[1]

            Gtemp = _geodesic_distance(mesh, faces[i], faces[j], edge)
            Atemp = _angular_distance(mesh, faces[i], faces[j])
            Gval.append(Gtemp)
            Aval.append(Atemp)
            row_indices.append(i)
            col_indices.append(j)
            # add symmetric entry
            Gval.append(Gtemp)
            Aval.append(Atemp)
            row_indices.append(j)
            col_indices.append(i)

        elif len(adj_faces) > 2:
            print("Edge with more than 2 adjacent faces: " + str(adj_faces) + "!")

    Gval = numpy.array(Gval)
    Aval = numpy.array(Aval)
    values = delta * Gval / numpy.mean(Gval) + \
             (1.0 - delta) * Aval / numpy.mean(Aval)

    # create sparse matrix
    distance_matrix = scipy.sparse.csr_matrix(
        (values, (row_indices, col_indices)), shape=(l, l))
    return distance_matrix


def _create_affinity_matrix(mesh):
    """Create the adjacency matrix of the given mesh"""

    l = len(mesh.polygons)
    print("mesh_segmentation: Creating distance matrices...")
    distance_matrix = _create_distance_matrix(mesh)

    print("mesh_segmentation: Finding shortest paths between all faces...")
    # for each non adjacent pair of faces find shortest path of adjacent faces
    W = scipy.sparse.csgraph.dijkstra(distance_matrix)
    inf_indices = numpy.where(numpy.isinf(W))
    W[inf_indices] = 0

    print("mesh_segmentation: Creating affinity matrix...")
    # change distance entries to similarities
    sigma = W.sum()/(l ** 2)
    den = 2 * (sigma ** 2)
    W = numpy.exp(-W/den)
    W[inf_indices] = 0
    numpy.fill_diagonal(W, 1)

    return W


def _initial_guess(Q, k):
    """Computes an initial guess for the cluster-centers

    Chooses indices of the observations with the least association to each
    other in a greedy manner. Q is the association matrix of the observations.
    """

    # choose the pair of indices with the lowest association to each other
    min_indices = numpy.unravel_index(numpy.argmin(Q), Q.shape)

    chosen = [min_indices[0], min_indices[1]]
    for _ in range(2,k):
        # Take the maximum of the associations to the already chosen indices for
        # every index. The index with the lowest result in that therefore is the
        # least similar to the already chosen pivots so we take it.
        # Note that we will never get an index that was already chosen because
        # an index always has the highest possible association 1.0 to itself
        new_index = numpy.argmin(numpy.max(Q[chosen,:], axis=0))
        chosen.append(new_index)

    return chosen


def segment_mesh(mesh, k, coefficients, action, ev_method, kmeans_init):
    """Segments the given mesh into k clusters and performs the given
    action for each cluster
    """

    # set coefficients
    global delta
    global eta
    delta, eta = coefficients

    # affinity matrix
    W = _create_affinity_matrix(mesh)
    print("mesh_segmentation: Calculating graph laplacian...")
    # degree matrix
    Dsqrt = numpy.sqrt(numpy.reciprocal(W.sum(1)))
    # graph laplacian
    L = ((W * Dsqrt).transpose() * Dsqrt).transpose()

    print("mesh_segmentation: Calculating eigenvectors...")
    # get eigenvectors
    if ev_method == 'dense':
        _, V = scipy.linalg.eigh(L, eigvals = (L.shape[0] - k, L.shape[0] - 1))
    else:
        _, V = scipy.sparse.linalg.eigsh(L, k)
    # normalize each row to unit length
    V /= numpy.linalg.norm(V, axis=1)[:,None]

    if kmeans_init == 'kmeans++':
        print("mesh_segmentation: Applying kmeans...")
        _, idx = scipy.cluster.vq.kmeans2(V, k, minit='++', iter=50)
    else:
        print("mesh_segmentation: Preparing kmeans...")
        # compute association matrix
        Q = V.dot(V.transpose())
        # compute initial guess for clustering
        initial_centroids = _initial_guess(Q, k)

        print("mesh_segmentation: Applying kmeans...")
        _, idx = scipy.cluster.vq.kmeans2(V, V[initial_centroids,:], iter=50)

    print("mesh_segmentation: Done clustering!")
    # perform action with the clustering result
    if action:
        action(mesh, k, idx)
