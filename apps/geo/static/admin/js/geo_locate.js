(function () {
    'use strict';

    function getMapId(mapDivId) {
        if (mapDivId.endsWith('-div-map')) return mapDivId.slice(0, -8) + '-map';
        if (mapDivId.endsWith('-map')) return mapDivId;
        return mapDivId + '-map';
    }

    function getLeafletMap(mapDivId) {
        var mapId = getMapId(mapDivId);
        return window['leafletmap' + mapId];
    }

    function flyMapTo(map, lat, lng, approximate) {
        if (!map) return false;
        map.flyTo([lat, lng], approximate ? 14 : 17, { duration: 0.9 });
        return true;
    }

    function dropMarker(map, lat, lng, approximate) {
        if (!map) return;
        var existing = document.querySelector('.geo-locate-marker[data-map="' + map._container.id + '"]');
        if (existing) existing.remove();

        var color = approximate ? '#f5a623' : '#4285f4';
        var ringColor = approximate ? 'rgba(245,166,35,0.20)' : 'rgba(66,133,244,0.20)';
        var ringEdge = approximate ? 'rgba(245,166,35,0.60)' : 'rgba(66,133,244,0.60)';

        var ring = L.circleMarker([lat, lng], {
            radius: 18,
            color: ringEdge,
            fillColor: ringColor,
            fillOpacity: 1,
            weight: 1.5,
            className: 'geo-locate-marker'
        }).addTo(map);
        ring._container.setAttribute('data-map', map._container.id);

        var dot = L.circleMarker([lat, lng], {
            radius: 9,
            color: '#ffffff',
            fillColor: color,
            fillOpacity: 1,
            weight: 2.5,
            className: 'geo-locate-marker'
        }).addTo(map);
        dot._container.setAttribute('data-map', map._container.id);

        setTimeout(function () {
            map.removeLayer(ring);
            map.removeLayer(dot);
        }, 5000);
    }

    function locateByIP(onDone, onFail) {
        fetch('https://ipapi.co/json/')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.latitude && data.longitude) {
                    onDone(data.latitude, data.longitude);
                } else {
                    onFail('IP geolocation returned no coordinates.');
                }
            })
            .catch(function () {
                onFail('IP geolocation request failed.');
            });
    }

    function injectButton(mapDiv) {
        if (mapDiv.querySelector('.geo-locate-btn')) return;

        var map = getLeafletMap(mapDiv.id);
        if (!map) return;

        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'geo-locate-btn';
        btn.title = 'Centre map on your current location';
        btn.innerHTML = '&#128205; Use My Location';

        Object.assign(btn.style, {
            position: 'absolute',
            top: '10px',
            right: '10px',
            zIndex: '1000',
            background: '#ffffff',
            border: '2px solid #0078d4',
            borderRadius: '6px',
            padding: '7px 14px',
            cursor: 'pointer',
            fontSize: '13px',
            fontWeight: '600',
            color: '#0078d4',
            boxShadow: '0 2px 8px rgba(0,0,0,0.25)',
            transition: 'background 0.15s, color 0.15s',
        });

        btn.addEventListener('mouseenter', function () {
            if (!btn.disabled) {
                btn.style.background = '#0078d4';
                btn.style.color = '#ffffff';
            }
        });
        btn.addEventListener('mouseleave', function () {
            if (!btn.disabled) {
                btn.style.background = '#ffffff';
                btn.style.color = '#0078d4';
            }
        });

        mapDiv.style.position = 'relative';
        mapDiv.appendChild(btn);

        function setLoading(label) {
            btn.innerHTML = '&#8987; ' + label;
            btn.disabled = true;
            btn.style.background = '#ffffff';
            btn.style.color = '#666';
            btn.style.borderColor = '#aaa';
        }
        function setSuccess(label, color) {
            color = color || '#34a853';
            btn.innerHTML = '&#10003; ' + label;
            btn.disabled = false;
            btn.style.background = '#ffffff';
            btn.style.color = color;
            btn.style.borderColor = color;
            setTimeout(resetBtn, 3000);
        }
        function setError(msg) {
            alert(msg);
            resetBtn();
        }
        function resetBtn() {
            btn.innerHTML = '&#128205; Use My Location';
            btn.disabled = false;
            btn.style.background = '#ffffff';
            btn.style.color = '#0078d4';
            btn.style.borderColor = '#0078d4';
        }

        btn.addEventListener('click', function () {
            setLoading('Locating\u2026');

            if (navigator.geolocation) {
                navigator.geolocation.getCurrentPosition(
                    function (pos) {
                        var lat = pos.coords.latitude;
                        var lng = pos.coords.longitude;
                        flyMapTo(map, lat, lng, false);
                        dropMarker(map, lat, lng, false);
                        setSuccess('Located (GPS)');
                    },
                    function () {
                        setLoading('Trying IP location\u2026');
                        locateByIP(
                            function (lat, lng) {
                                flyMapTo(map, lat, lng, true);
                                dropMarker(map, lat, lng, true);
                                setSuccess('Located \u2248 (by IP, approximate)', '#f5a623');
                            },
                            function () {
                                setError('GPS blocked and IP lookup failed.');
                            }
                        );
                    },
                    { enableHighAccuracy: true, timeout: 8000, maximumAge: 0 }
                );
            } else {
                setLoading('Trying IP location\u2026');
                locateByIP(
                    function (lat, lng) {
                        flyMapTo(map, lat, lng, true);
                        dropMarker(map, lat, lng, true);
                        setSuccess('Located \u2248 (by IP)', '#f5a623');
                    },
                    function () { setError('Could not determine location.'); }
                );
            }
        });
    }

    function initAll() {
        var mapDivs = document.querySelectorAll('div[id$="-div-map"]');
        if (mapDivs.length === 0) {
            mapDivs = document.querySelectorAll('div.leaflet-container');
        }
        mapDivs.forEach(injectButton);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { setTimeout(initAll, 600); });
    } else {
        setTimeout(initAll, 600);
    }
})();
