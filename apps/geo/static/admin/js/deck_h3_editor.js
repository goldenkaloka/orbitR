/**
 * deck_h3_editor.js
 * Premium H3-based zone editor using deck.gl
 * Features: Auto-geolocation, Dark Mode aesthetics, Interactive Painting
 */

(function () {
    'use strict';

    const RESOLUTION = 9;
    const DEFAULT_CENTER = [39.2026, -6.7924]; // Dar es Salaam
    
    let deckInstance = null;
    let selectedCells = new Set();
    let isEditing = false;
    let mapInstance = null;

    // Load dependencies
    const scripts = [
        'https://unpkg.com/deck.gl@8.9.33/dist.min.js',
        'https://unpkg.com/h3-js@4.1.0/dist/h3-js.umd.js',
        'https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.js'
    ];

    const styles = [
        'https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.css',
        'https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap'
    ];

    function loadDependencies(callback) {
        let loaded = 0;
        styles.forEach(url => {
            const link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = url;
            document.head.appendChild(link);
        });

        scripts.forEach(url => {
            const script = document.createElement('script');
            script.src = url;
            script.onload = () => {
                loaded++;
                if (loaded === scripts.length) callback();
            };
            document.head.appendChild(script);
        });
    }

    function initDeckEditor(mapContainer, textArea) {
        // Create a wrapper for the new premium experience
        const wrapper = document.createElement('div');
        wrapper.className = 'premium-map-wrapper';
        Object.assign(wrapper.style, {
            width: '100%',
            height: '600px',
            position: 'relative',
            marginTop: '20px',
            marginBottom: '20px',
            borderRadius: '12px',
            boxShadow: '0 10px 30px rgba(0,0,0,0.15)',
            overflow: 'hidden',
            fontFamily: "'Inter', sans-serif",
            border: '1px solid #333'
        });

        // Add custom controls
        const controls = document.createElement('div');
        Object.assign(controls.style, {
            position: 'absolute',
            top: '20px',
            left: '20px',
            zIndex: '10',
            display: 'flex',
            flexDirection: 'column',
            gap: '10px'
        });

        const mainBtn = createControlBtn('🎨 Paint Zones: OFF', '#fff', '#1a1a1a');
        mainBtn.onclick = () => {
            if (!deckInstance) {
                console.error('Map not initialized yet');
                return;
            }
            isEditing = !isEditing;
            mainBtn.innerText = `🎨 Paint Zones: ${isEditing ? 'ON' : 'OFF'}`;
            mainBtn.style.background = isEditing ? '#4285f4' : '#1a1a1a';
            mainBtn.style.color = '#fff';
            updateLayers();
        };

        const locateBtn = createControlBtn('📍 Locate Me', '#fff', '#1a1a1a');
        locateBtn.onclick = () => {
            if (!deckInstance) {
                console.error('Map not initialized yet');
                return;
            }
            autoLocate();
        };

        const clearBtn = createControlBtn('🗑️ Clear All', '#fff', '#1a1a1a');
        clearBtn.onclick = () => {
            if (!deckInstance) {
                console.error('Map not initialized yet');
                return;
            }
            if (confirm("Are you sure you want to clear all hexagons?")) {
                selectedCells.clear();
                updateLayers();
                syncToDjango();
            }
        };

        controls.appendChild(mainBtn);
        controls.appendChild(locateBtn);
        controls.appendChild(clearBtn);
        wrapper.appendChild(controls);

        // MapLibre container
        const mapContainerDiv = document.createElement('div');
        mapContainerDiv.style.width = '100%';
        mapContainerDiv.style.height = '100%';
        wrapper.appendChild(mapContainerDiv);

        // Hide original map container to clean up space
        mapContainer.style.display = 'none';
        mapContainer.parentNode.insertBefore(wrapper, mapContainer);

        // Initialize MapLibre with a premium Dark Style
        var maplibreGlobal = window.maplibregl || window.maplibre;
        if (!maplibreGlobal) {
            console.error('MapLibre GL not loaded');
            return;
        }

        mapInstance = new maplibreGlobal.Map({
            container: mapContainerDiv,
            style: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
            center: DEFAULT_CENTER,
            zoom: 13,
            pitch: 45
        });

        deckInstance = new deck.DeckGL({
            container: mapContainerDiv,
            maplibregl: maplibreGlobal,
            initialViewState: {
                longitude: DEFAULT_CENTER[0],
                latitude: DEFAULT_CENTER[1],
                zoom: 13,
                pitch: 45,
                bearing: 0
            },
            controller: true,
            onViewStateChange: ({viewState}) => {
                if (mapInstance) {
                    mapInstance.jumpTo({
                        center: [viewState.longitude, viewState.latitude],
                        zoom: viewState.zoom,
                        bearing: viewState.bearing,
                        pitch: viewState.pitch
                    });
                }
            },
            layers: []
        });

        function createControlBtn(text, color, bg) {
            const b = document.createElement('button');
            b.type = 'button';
            b.innerText = text;
            Object.assign(b.style, {
                padding: '10px 16px',
                border: 'none',
                borderRadius: '8px',
                background: bg,
                color: color,
                cursor: 'pointer',
                fontSize: '14px',
                fontWeight: '600',
                boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
                transition: 'all 0.2s ease',
                display: 'flex',
                alignItems: 'center',
                gap: '8px'
            });
            b.onmouseover = () => b.style.transform = 'translateY(-2px)';
            b.onmouseout = () => b.style.transform = 'translateY(0)';
            return b;
        }

        function autoLocate() {
            if (!deckInstance) return;
            if (navigator.geolocation) {
                navigator.geolocation.getCurrentPosition(pos => {
                    const {latitude, longitude} = pos.coords;
                    deckInstance.setProps({
                        initialViewState: {
                            longitude,
                            latitude,
                            zoom: 15,
                            transitionDuration: 1000,
                            transitionInterpolator: new deck.FlyToInterpolator()
                        }
                    });
                });
            }
        }

        function updateLayers() {
            if (!deckInstance) return;
            const data = Array.from(selectedCells).map(hex => ({hex}));
            
            const layers = [
                new deck.H3HexagonLayer({
                    id: 'h3-layer',
                    data: data,
                    getH3Hexagon: d => d.hex,
                    getFillColor: [66, 133, 244, 140],
                    getLineColor: [255, 255, 255, 200],
                    lineWidthMinPixels: 1,
                    pickable: true,
                    extruded: true,
                    getElevation: 20,
                    material: {
                        ambient: 0.64,
                        diffuse: 0.6,
                        shininess: 32,
                        specularColor: [51, 51, 51]
                    }
                })
            ];

            deckInstance.setProps({
                layers,
                onClick: (info) => {
                    if (!isEditing) return;
                    
                    let hex;
                    if (info.layer && info.layer.id === 'h3-layer') {
                        hex = info.object.hex;
                    } else if (info.coordinate) {
                        hex = h3.latLngToCell(info.coordinate[1], info.coordinate[0], RESOLUTION);
                    }

                    if (hex) {
                        if (selectedCells.has(hex)) {
                            selectedCells.delete(hex);
                        } else {
                            selectedCells.add(hex);
                        }
                        updateLayers();
                        syncToDjango();
                    }
                }
            });
        }

        function syncToDjango() {
            if (selectedCells.size === 0) {
                textArea.value = '';
                return;
            }
            const multiPoly = h3.cellsToMultiPolygon(Array.from(selectedCells), true);
            const geojson = {
                type: "MultiPolygon",
                coordinates: multiPoly
            };
            textArea.value = JSON.stringify(geojson);
            textArea.dispatchEvent(new Event('change', { bubbles: true }));
        }

        // Try auto-locate on start
        setTimeout(autoLocate, 1000);
        updateLayers();
    }

    function start() {
        const textArea = document.querySelector('#id_boundary');
        const mapContainer = document.querySelector('#id_boundary-div-map') ||
                           document.querySelector('#id_boundary_map') ||
                           document.querySelector('#id_boundary_map_container');

        if (textArea && mapContainer) {
            initDeckEditor(mapContainer, textArea);
        }
    }

    loadDependencies(start);

})();
