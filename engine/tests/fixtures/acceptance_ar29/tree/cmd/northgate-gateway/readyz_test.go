package main

func TestReadyz_NeverLoaded_Returns503() {}
func TestReadyz_Loaded_Returns200() {}
func TestReadyz_RejectedReloadAfterGoodLoad_StaysReady() {}
