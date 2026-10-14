//go:build windows

package httpserver

import (
	"syscall"
	"unsafe"
)

var (
	kernel32            = syscall.NewLazyDLL("kernel32.dll")
	getDiskFreeSpaceExW = kernel32.NewProc("GetDiskFreeSpaceExW")
)

func readDiskUsage(path string) (totalBytes uint64, freeBytes uint64, err error) {
	pathPtr, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return 0, 0, err
	}
	var freeBytesAvailable, totalBytesAvailable, totalFreeBytes uint64
	ret, _, callErr := getDiskFreeSpaceExW.Call(
		uintptr(unsafe.Pointer(pathPtr)),
		uintptr(unsafe.Pointer(&freeBytesAvailable)),
		uintptr(unsafe.Pointer(&totalBytesAvailable)),
		uintptr(unsafe.Pointer(&totalFreeBytes)),
	)
	if ret == 0 {
		return 0, 0, callErr
	}
	return totalBytesAvailable, freeBytesAvailable, nil
}
